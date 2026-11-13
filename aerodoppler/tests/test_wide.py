"""The default area is Iberia, and the free web service does not sweep it."""

import importlib.util
import json
import subprocess
import sys

from fastapi.testclient import TestClient

from aerodoppler.app import create_app
from aerodoppler.config import Settings
from aerodoppler.regions import PLACES, region_tiles, resolve_region
from aerodoppler.wide_search import filter_flights, free_tier_response, run_local_tiles, score_tile


def _settings():
    return Settings(
        access_token="x" * 24,
        cookie_secure=False,
        max_upload_bytes=1024 * 1024,
        max_duration_s=90,
        sample_rate=16000,
        max_radius_km=120,
        max_window_min=360,
        ffmpeg_timeout_s=20,
        history_timeout_s=25,
        max_history_pages=3,
        max_flights=40,
        lookback_h=72,
        analysis_timeout_s=30,
    )


def test_no_regional_hint_is_iberia_and_covers_the_listed_places():
    assert resolve_region(None) == "iberia"
    assert resolve_region("") == "iberia"
    assert resolve_region("unsure") == "iberia"
    assert resolve_region("no sé") == "iberia"
    plan = region_tiles("unsure")
    assert plan["id"] == "iberia"
    assert plan["tile_radius_km"] == 110
    assert plan["tile_count"] == len(plan["tiles"]) == 21
    for name, lat, lon in PLACES:
        assert any(_km(tile["lat"], tile["lon"], lat, lon) <= 110.01 for tile in plan["tiles"]), name


def test_free_web_refuses_the_default_area_without_calling_a_provider():
    result = free_tier_response(None, _settings(), uncertainty_h=2)
    assert result["status"] == "incomplete"
    assert result["reason"] == "wide_area_not_on_free_tier"
    assert result["verdict"] == "failed"
    assert result["show_point"] is False
    assert result["reported_point"] is None
    assert result["point_is_certain"] is False
    assert result["plan"]["fits_free_web"] is False
    assert result["plan"]["queries"] == result["plan"]["tile_count"] * (1 + 3)
    assert result["plan"]["time_bound_s"] == result["plan"]["queries"] * 25
    assert "adsb.aero" in result["message"]
    assert "varios GB" in result["message"]
    assert result["local_mode"]["plan_command"].startswith("python -m aerodoppler.wide_search")
    assert result["region"]["role"] != "micrófono"


def test_locate_api_without_coordinates_returns_the_plan():
    client = TestClient(create_app(_settings()))
    response = client.post(
        "/api/locate",
        json={"region": "unsure", "recording_start_local": "2026-11-07T23:00", "uncertainty_h": 2},
        headers={"Authorization": "Bearer " + ("x" * 24)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "wide_area_refused"
    assert body["plan"]["tile_count"] >= 8
    assert body["map"]["radius_km"] > 100
    assert "x" * 24 not in response.text


def test_time_and_type_filter_keeps_only_a_relevant_propeller():
    tile = {"name": "centro", "lat": 40.0, "lon": -4.0, "radius_km": 110}
    flights = [
        _flight("LEJOS", 50.0, 10.0, "piston", 1_000.0),
        _flight("REACTOR", 40.0, -4.0, "jet", 1_000.0, eligible=False),
        _flight("TARDE", 40.1, -4.0, "piston", 50_000.0),
        _flight("CERCA", 40.2, -3.9, "piston", 1_000.0),
    ]
    kept = filter_flights(flights, tile, 900.0, 2_000.0)
    assert [item["callsign"] for item in kept] == ["CERCA"]


def test_one_tile_keeps_the_honest_verdict():
    cases = _uncertainty()
    truth_lat, truth_lon = cases._offset(40.0, -4.0, 2500.0, 2000.0)
    from aerodoppler.physics import ecef

    observer = ecef(truth_lat, truth_lon, 220.0).reshape(3)
    flights = cases._crossing(truth_lat, truth_lon)
    ridges = [cases._ridge_from(observer, flight, 9_970.0) for flight in flights]
    tile = {"name": "sintetica", "lat": 40.0, "lon": -4.0, "radius_km": 8.0}
    found = score_tile(tile, flights, ridges[0], 9_970.0, 0.0, 220.0, events=ridges, budget=cases.BUDGET)
    assert found["verdict"] in {"ok", "weak", "ambiguous", "failed"}
    assert found["point_is_certain"] is False
    assert found["calibrated_confidence"] is False
    if found["show_point"]:
        error_m = cases._meters(found["reported_point"]["lat"], found["reported_point"]["lon"], truth_lat, truth_lon)
        assert error_m <= found["regions"]["90"]["radius_m"]


def test_a_stopped_local_sweep_does_not_query():
    from datetime import datetime, timezone

    def boom(*_args, **_kwargs):
        raise AssertionError("no debía consultar")

    report = run_local_tiles(
        [{"name": "una", "lat": 40.0, "lon": -4.0, "radius_km": 110}],
        datetime(2026, 11, 7, tzinfo=timezone.utc),
        datetime(2026, 11, 7, 1, tzinfo=timezone.utc),
        _settings(),
        {"time_s": list(range(12)), "frequency_hz": [180.0] * 12, "status": "accepted"},
        0.0,
        0.0,
        600.0,
        boom,
        max_seconds=-1,
    )
    assert report["provider_queries"] == 0
    assert report["incomplete"] is True
    assert report["archive_download"] is False


def test_plan_command_prints_the_cost_without_a_token():
    completed = subprocess.run(
        [sys.executable, "-m", "aerodoppler.wide_search", "--plan"],
        check=True,
        capture_output=True,
        text=True,
        cwd="/workspace",
        env={"PYTHONPATH": "/workspace/aerodoppler/src", "PATH": "/usr/bin"},
    )
    body = json.loads(completed.stdout)
    assert body["fits_free_web"] is False
    assert body["archive_download"] is False
    assert body["tile_count"] == region_tiles(None)["tile_count"]
    assert body["queries"] == body["tile_count"] * 4


def _uncertainty():
    spec = importlib.util.spec_from_file_location("uncertainty_cases", __file__.replace("test_wide.py", "test_uncertainty.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _flight(callsign, lat, lon, kind, t0, eligible=True):
    return {
        "callsign": callsign,
        "icao24": callsign.lower(),
        "engine_class": kind,
        "acoustic_eligible": eligible and kind != "jet",
        "points": [
            {"t": t0, "lat": lat, "lon": lon, "alt_m": 1000.0},
            {"t": t0 + 30, "lat": lat + 0.05, "lon": lon, "alt_m": 1000.0},
            {"t": t0 + 60, "lat": lat + 0.1, "lon": lon, "alt_m": 1000.0},
        ],
    }


def _km(lat1, lon1, lat2, lon2):
    import math

    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))
