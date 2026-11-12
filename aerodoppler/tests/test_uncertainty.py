"""Synthetic checks for the verdict, the 68/90 regions and a shared clock.

The curves are generated from the same propagation model the search uses.
They check internal consistency only. They are not a real-recording accuracy.
"""

import json
import math

import numpy as np

from aerodoppler.inverse import LocateBudget, locate_microphone
from aerodoppler.physics import ecef


PRIOR = {"lat": 40.0, "lon": -4.0, "radius_km": 8.0}
BUDGET = LocateBudget(spacing_m=1500.0, max_cells=120, max_seconds=25, max_clock_steps=31, max_flights=8)


def test_three_crossing_passes_are_ok_and_not_a_certain_point():
    truth_lat, truth_lon = _offset(40.0, -4.0, 2500.0, 2000.0)
    observer = ecef(truth_lat, truth_lon, 220.0).reshape(3)
    flights = _crossing(truth_lat, truth_lon)
    ridges = [_ridge_from(observer, flight, 9_970.0) for flight in flights]
    found = locate_microphone(ridges[0], flights, PRIOR, 9_970.0, 0.0, 220.0, budget=BUDGET, events=ridges)
    assert found["verdict"] == "ok"
    assert found["verdict_label"] == "Compatible"
    assert found["point_is_certain"] is False
    assert found["calibrated_confidence"] is False
    assert found["show_point"] is True
    assert found["reported_point"]["role"]
    error_m = _meters(found["reported_point"]["lat"], found["reported_point"]["lon"], truth_lat, truth_lon)
    radius_68 = found["regions"]["68"]["radius_m"]
    radius_90 = found["regions"]["90"]["radius_m"]
    assert radius_68 > 0
    assert radius_90 >= radius_68
    assert error_m <= radius_90
    assert error_m < found["resolution_m"] * 4
    json.dumps(found)


def test_parallel_passes_are_ambiguous_and_draw_no_winner():
    truth_lat, truth_lon = _offset(40.0, -4.0, 2500.0, 2000.0)
    observer = ecef(truth_lat, truth_lon, 220.0).reshape(3)
    south_a = _offset(truth_lat, truth_lon, -2200.0, 0.0)
    south_b = _offset(truth_lat, truth_lon, -3400.0, 0.0)
    flights = [
        _track("PARALELA", south_a[0], south_a[1], 90.0, 10_000.0),
        _track("PARALEB", south_b[0], south_b[1], 90.0, 10_500.0),
    ]
    ridges = [_ridge_from(observer, flight, 9_970.0) for flight in flights]
    found = locate_microphone(ridges[0], flights, PRIOR, 9_970.0, 0.0, 220.0, budget=BUDGET, events=ridges)
    assert found["verdict"] == "ambiguous"
    assert found["verdict_label"] == "Ambiguo"
    assert found["show_point"] is False
    assert found["reported_point"] is None
    assert found["point_is_certain"] is False
    assert found["regions"]["90"]["radius_m"] >= found["regions"]["68"]["radius_m"] > 0
    assert any("mínimo" in flag["message"] or "lado" in flag["message"] or "parte de la zona" in flag["message"] for flag in found["flags"])


def test_unmatched_clock_fails_without_a_region():
    truth_lat, truth_lon = _offset(40.0, -4.0, 2500.0, 2000.0)
    observer = ecef(truth_lat, truth_lon, 220.0).reshape(3)
    flights = _crossing(truth_lat, truth_lon)[:2]
    ridges = [_ridge_from(observer, flight, 9_970.0) for flight in flights]
    found = locate_microphone(ridges[0], flights, PRIOR, 9_970.0 + 5 * 3600.0, 0.0, 220.0, budget=BUDGET, events=ridges)
    assert found["verdict"] == "failed"
    assert found["verdict_label"] == "Insuficiente"
    assert found["show_point"] is False
    assert found["reported_point"] is None
    assert found["point_is_certain"] is False
    assert found["regions"]["68"]["radius_m"] is None
    assert found["regions"]["90"]["radius_m"] is None
    assert found["clock"]["shared"] is False


def test_two_passes_recover_an_unknown_shared_clock():
    truth_lat, truth_lon = _offset(40.0, -4.0, 2500.0, 2000.0)
    observer = ecef(truth_lat, truth_lon, 220.0).reshape(3)
    flights = _crossing(truth_lat, truth_lon)[:2]
    true_start = 9_970.0
    bias = 18.0
    ridges = [_ridge_from(observer, flight, true_start) for flight in flights]
    found = locate_microphone(
        ridges[0], flights, PRIOR, true_start - bias, 30.0, 220.0, budget=BUDGET, events=ridges,
    )
    assert found["clock"]["shared"] is True
    assert abs(found["clock"]["offset_s"] - bias) <= found["clock"]["step_s"] + 1e-6
    assert found["verdict"] in {"weak", "ok", "ambiguous"}
    assert found["point_is_certain"] is False
    assert found["events_explained"] >= 2
    center = found["reported_point"] or found["hypotheses"][0]
    error_m = _meters(center["lat"], center["lon"], truth_lat, truth_lon)
    assert found["regions"]["90"]["radius_m"] >= found["regions"]["68"]["radius_m"] > 0
    assert error_m <= found["regions"]["90"]["radius_m"]
    if found["verdict"] == "ambiguous":
        assert found["show_point"] is False
        assert found["reported_point"] is None


def test_one_pass_stays_ambiguous_with_regions():
    truth = ecef(40.035, -4.0, 220.0).reshape(3)
    flight = _track("CERCA", 40.015, -4.0, 90.0, 1_000_000.0)
    start = flight["points"][0]["t"] - 30.0
    ridge = _ridge_from(truth, flight, start)
    found = locate_microphone(
        ridge, [flight], {"lat": 40.0, "lon": -4.0, "radius_km": 12.0}, start, 20.0, 220.0,
        budget=LocateBudget(spacing_m=2000.0, max_cells=80, max_seconds=8),
    )
    assert found["status"] == "ambiguous"
    assert found["verdict"] == "ambiguous"
    assert found["show_point"] is False
    assert found["reported_point"] is None
    assert found["clock"]["shared"] is False
    assert found["regions"]["90"]["radius_m"] >= found["regions"]["68"]["radius_m"] > 0


def _crossing(lat, lon):
    north = _offset(lat, lon, 1800.0, 0.0)
    east = _offset(lat, lon, 0.0, 1600.0)
    southwest = _offset(lat, lon, -1200.0, -900.0)
    return [
        _track("NORTE", north[0], north[1], 90.0, 10_000.0),
        _track("ESTE", east[0], east[1], 0.0, 10_400.0),
        _track("SURO", southwest[0], southwest[1], 50.0, 10_800.0),
    ]


def _track(callsign, lat, lon, heading, t0):
    points = []
    for index in range(41):
        distance = (index - 20) * 160.0
        north = distance * math.cos(math.radians(heading))
        east = distance * math.sin(math.radians(heading))
        point_lat, point_lon = _offset(lat, lon, north, east)
        points.append({"t": t0 + index * 2.0, "lat": point_lat, "lon": point_lon, "alt_m": 1600.0})
    return {
        "flight_id": callsign,
        "icao24": callsign.lower(),
        "callsign": callsign,
        "engine_class": "piston",
        "acoustic_eligible": True,
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


def _offset(lat, lon, north_m, east_m):
    return (
        lat + north_m / 110570.0,
        lon + east_m / (111320.0 * math.cos(math.radians(lat))),
    )


def _meters(lat1, lon1, lat2, lon2):
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))
