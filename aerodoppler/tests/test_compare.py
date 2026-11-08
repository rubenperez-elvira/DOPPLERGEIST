import numpy as np

from aerodoppler.compare import compare_tracks


def _flight(callsign, lat, lon, kind="piston", eligible=True):
    start = 1_794_088_800.0
    points = []
    for index in range(31):
        stamp = start + index * 2.0
        along = (index - 15) * 180.0
        points.append({"t": stamp, "lat": lat + along / 110570.0, "lon": lon, "alt_m": 1800.0})
    return {
        "flight_id": callsign,
        "icao24": "aaaaaa",
        "callsign": callsign,
        "icao_type": "C172" if eligible else "B738",
        "engine_class": kind,
        "acoustic_eligible": eligible,
        "points": points,
    }


def test_delay_and_ranking_do_not_claim_identity():
    observer = {"lat": 40.97786, "lon": -5.60845, "alt_m": 827.0, "altitude_note": "prueba"}
    true = _flight("CERCA", 40.995, -5.60845)
    other = _flight("LEJOS", 41.35, -5.2)
    jet = _flight("REACTOR", 40.995, -5.60845, "jet", False)
    preview = compare_tracks(observer, [true], None, None, 160.0)
    curve = preview["flights"][0]
    assert curve["propagation_delay_s"] > 0
    assert curve["closest_m"] > 500
    origin = curve["curve_preview"][0]["receive_unix"]
    ridge = {
        "time_s": [point["receive_unix"] - origin for point in curve["curve_preview"]],
        "frequency_hz": [point["frequency_hz"] for point in curve["curve_preview"]],
    }
    ranked = compare_tracks(observer, [other, jet, true], ridge, origin, 160.0)
    assert ranked["unique_identification"] is False
    scored = [item for item in ranked["flights"] if item.get("rank_status") == "scored"]
    assert scored
    best = min(scored, key=lambda item: item["rmse_hz"])
    assert best["callsign"] == "CERCA"
    assert best["rmse_hz"] < 1.0
    jet_row = next(item for item in ranked["flights"] if item["callsign"] == "REACTOR")
    assert jet_row["rank_status"] == "excluded_from_acoustic_rank"
    assert jet_row["closest_m"] > 0
    assert "retardo" in jet_row["message"] or "ranking" in jet_row["message"]


def test_missing_clock_does_not_invent_alignment():
    observer = {"lat": 40.97786, "lon": -5.60845, "alt_m": 800.0}
    result = compare_tracks(observer, [_flight("CERCA", 40.995, -5.60845)], {"time_s": [0, 1, 2, 3, 4, 5], "frequency_hz": [100, 99, 98, 97, 96, 95]}, None, None)
    assert result["status"] == "needs_recording_clock"
    assert all(item.get("rmse_hz") is None for item in result["flights"])
    assert np.isfinite(result["flights"][0]["closest_m"])
