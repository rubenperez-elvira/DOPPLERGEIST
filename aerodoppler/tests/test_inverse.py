import math
import os

import numpy as np
import pytest

from aerodoppler.inverse import LocateBudget, locate_microphone
from aerodoppler.physics import ecef


PRIOR = {"lat": 40.0, "lon": -4.0, "radius_km": 12.0}


def _flight(callsign, lat, lon, kind="piston", eligible=True, t0=1_000_000.0):
    points = []
    for index in range(41):
        stamp = t0 + index * 2.0
        east = (index - 20) * 160.0
        points.append({
            "t": stamp,
            "lat": lat,
            "lon": lon + east / (111320.0 * math.cos(math.radians(lat))),
            "alt_m": 1600.0,
        })
    return {
        "flight_id": callsign,
        "icao24": callsign.lower(),
        "callsign": callsign,
        "engine_class": kind,
        "acoustic_eligible": eligible,
        "points": points,
    }


def _ridge_from(observer, flight, start, f0=180.0):
    points = flight["points"]
    times = np.array([point["t"] for point in points])
    xyz = ecef([point["lat"] for point in points], [point["lon"] for point in points], [point["alt_m"] for point in points])
    velocity = np.gradient(xyz, times, axis=0)
    relative = xyz - observer
    distance = np.linalg.norm(relative, axis=1)
    radial = np.sum(velocity * relative, axis=1) / distance
    receive = times + distance / 343.0
    shape = 343.0 / (343.0 + radial)
    step = max(1, len(receive) // 40)
    chosen = slice(2, -2, step)
    return {
        "time_s": (receive[chosen] - start).tolist(),
        "frequency_hz": (f0 * shape[chosen]).tolist(),
        "status": "accepted",
    }


def test_blind_offset_is_a_hypothesis_and_not_the_prior_center():
    truth = ecef(40.035, -4.0, 220.0).reshape(3)
    flight = _flight("CERCA", 40.015, -4.0)
    start = flight["points"][0]["t"] - 30.0
    ridge = _ridge_from(truth, flight, start)
    found = locate_microphone(
        ridge, [flight], PRIOR, start, 20.0, 220.0,
        budget=LocateBudget(spacing_m=2000.0, max_cells=80, max_seconds=8),
    )
    assert found["unique_identification"] is False
    assert found["prior_is_result"] is False
    assert found["reported_point"] is None
    assert found["status"] == "ambiguous"
    assert found["hypotheses"]
    assert found["message"]
    nearest = min(found["hypotheses"], key=lambda item: _km(item["lat"], item["lon"], 40.035, -4.0))
    assert _km(nearest["lat"], nearest["lon"], 40.035, -4.0) < found["resolution_m"] / 1000.0 + 1.5
    assert _km(nearest["lat"], nearest["lon"], 40.035, -4.0) < _km(PRIOR["lat"], PRIOR["lon"], 40.035, -4.0)


def test_solver_rejects_a_supplied_true_location():
    with pytest.raises(ValueError):
        locate_microphone({"time_s": [0, 1, 2, 3, 4, 5], "frequency_hz": [1, 1, 1, 1, 1, 1], "status": "accepted"}, [], {"lat": 1, "lon": 2, "radius_km": 5, "truth_lat": 1}, 0, 0, 10)


def test_no_event_no_eligible_and_partial_do_not_invent_a_point():
    empty = locate_microphone(None, [_flight("CERCA", 40.02, -4.0)], PRIOR, 0, 60, 200)
    assert empty["status"] == "no_usable_event"
    assert empty["hypotheses"] == []
    rejected = locate_microphone({"status": "rejected", "time_s": [0, 5, 10], "frequency_hz": [100, 90, 80]}, [_flight("CERCA", 40.02, -4.0)], PRIOR, 0, 60, 200)
    assert rejected["status"] == "no_usable_event"
    jets = locate_microphone(
        {"status": "accepted", "time_s": list(range(8)), "frequency_hz": [200] * 8},
        [_flight("REACTOR", 40.02, -4.0, "jet", False)],
        PRIOR, 0, 60, 200,
    )
    assert jets["status"] == "no_eligible_aircraft"
    assert jets["flights_received"] == 1
    assert jets["excluded_flights"]["jet"] == 1
    assert jets["zero_flights_claim"] is False
    assert jets["hypotheses"] == []
    missing = locate_microphone(
        {"status": "accepted", "time_s": list(range(8)), "frequency_hz": [200] * 8},
        [], PRIOR, 0, 60, 200,
    )
    assert missing["status"] == "no_eligible_aircraft"
    assert missing["reason"] == "no_flights_in_query"
    partial = locate_microphone(
        {"status": "accepted", "time_s": list(range(8)), "frequency_hz": [200] * 8},
        [_flight("CERCA", 40.02, -4.0)], PRIOR, 0, 60, 200, history_status="api_error",
    )
    assert partial["status"] == "incomplete"
    assert partial["reported_point"] is None


def test_resource_limits_abstain_without_a_center_fix():
    flight = _flight("CERCA", 40.015, -4.0)
    start = flight["points"][0]["t"]
    ridge = _ridge_from(ecef(40.035, -4.0, 220.0).reshape(3), flight, start)
    timed = locate_microphone(ridge, [flight], PRIOR, start, 20.0, 220.0, budget=LocateBudget(max_seconds=-1))
    assert timed["status"] == "incomplete"
    assert timed["reason"] == "time_budget"
    assert timed["hypotheses"] == []
    coarse = locate_microphone(ridge, [flight], {"lat": 40.0, "lon": -4.0, "radius_km": 20}, start, 20.0, 220.0, budget=LocateBudget(max_cells=1, spacing_m=2500))
    assert coarse["status"] == "incomplete"
    assert coarse["reason"] == "grid_too_coarse"
    assert coarse["hypotheses"] == []
    assert coarse["reported_point"] is None


def test_private_clip_is_not_a_location(monkeypatch):
    path = os.environ.get("AERDOPPLER_PRIVATE_WAV")
    if not path or not os.path.isfile(path):
        pytest.skip("grabación privada no montada")
    from aerodoppler.audio_engine import analyze_samples, decode_audio

    samples = decode_audio("ffmpeg", "ffprobe", __import__("pathlib").Path(path), 16000, 90, 25)
    audio = analyze_samples(samples, 16000)
    found = locate_microphone(audio.get("ridge") and {**audio["ridge"], "status": audio["status"]}, [_flight("CERCA", 40.02, -4.0)], PRIOR, 0, 900, 800)
    assert found["status"] == "no_usable_event"
    assert found["hypotheses"] == []
    assert found["reported_point"] is None


def _km(lat1, lon1, lat2, lon2):
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))
