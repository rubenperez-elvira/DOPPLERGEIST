"""The default area is Iberia, and the free web service does not sweep it."""

import importlib.util
import json
import subprocess
import sys
import time

from fastapi.testclient import TestClient

from aerodoppler.app import create_app
from aerodoppler.config import Settings
from aerodoppler.regions import PLACES, region_tiles, resolve_region
from aerodoppler.wide_search import filter_flights, free_tier_response, plan_cost, run_local_tiles, score_tile


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


def test_melilla_fits_one_circle_and_badajoz_does_not():
    from aerodoppler.provinces import _km, _samples, cover_selection, neighbors_of, province_record

    small = plan_cost("melilla", _settings())
    assert small["fits_free_web"] is True
    assert small["tile_count"] == 1
    assert small["queries"] == 4
    assert small["search_bound_s"] <= 120
    samples = _samples(province_record("melilla")["rings"])
    assert len(samples) > 1
    tile = small["tiles"][0]
    assert all(_km(tile["lat"], tile["lon"], lat, lon) <= 110.01 for lon, lat in samples)

    large = plan_cost("badajoz", _settings())
    assert large["fits_free_web"] is False
    assert large["tile_count"] >= 2
    assert large["search_bound_s"] > 120
    centroid_lat = sum(lat for _lon, lat in _samples(province_record("badajoz")["rings"])) / large["samples"]
    centroid_lon = sum(lon for lon, _lat in _samples(province_record("badajoz")["rings"])) / large["samples"]
    farthest = max(_km(centroid_lat, centroid_lon, lat, lon) for lon, lat in _samples(province_record("badajoz")["rings"]))
    assert farthest > 110
    assert all(
        min(_km(tile["lat"], tile["lon"], lat, lon) for tile in large["tiles"]) <= 110.01
        for lon, lat in _samples(province_record("badajoz")["rings"])
    )
    assert "toledo" in neighbors_of("madrid")
    assert "barcelona" not in neighbors_of("madrid")


def test_contiguous_provinces_are_planned_together_and_do_not_partially_run():
    pair = plan_cost("madrid,toledo", _settings())
    assert pair["tile_count"] >= 2
    assert pair["fits_free_web"] is False
    assert "Madrid" in pair["label"] and "Toledo" in pair["label"]
    basque = plan_cost("cc:PV", _settings())
    assert basque["tile_count"] == 1
    assert basque["fits_free_web"] is True
    try:
        plan_cost("madrid,barcelona", _settings())
    except ValueError as error:
        assert "contiguo" in str(error)
    else:
        raise AssertionError("madrid y barcelona no son contiguas")
    client = TestClient(create_app(_settings()))
    refused = client.post(
        "/api/locate",
        json={"region": "badajoz", "recording_start_local": "2026-11-07T23:00", "uncertainty_h": 1},
        headers={"Authorization": "Bearer " + ("x" * 24)},
    )
    assert refused.status_code == 200
    body = refused.json()
    assert body["mode"] == "wide_area_refused"
    assert body["job_id"] if False else "job_id" not in body
    assert body["plan"]["executed"] if False else body["show_point"] is False


def test_a_fitting_province_runs_the_whole_circle_and_not_a_partial_list():
    from datetime import datetime, timezone

    from aerodoppler.wide_search import execute_covered_plan

    plan = plan_cost("melilla", _settings())
    opener = _Opener({
        "/data-range": {"first_date": "2025-05-05", "last_date": "2026-11-08"},
        "/query": {"flights": [], "cursor": None},
    })
    found = execute_covered_plan(
        plan,
        datetime(2026, 11, 7, 22, tzinfo=timezone.utc),
        datetime(2026, 11, 7, 23, tzinfo=timezone.utc),
        _settings(),
        {"status": "accepted", "time_s": [0, 1, 2, 3, 4, 5, 6, 7], "frequency_hz": [180] * 8},
        datetime(2026, 11, 7, 22, 30, tzinfo=timezone.utc).timestamp(),
        1800.0,
        600.0,
        opener,
    )
    assert found["executed"] is True
    assert found["verdict"] == "failed"
    assert found["show_point"] is False
    assert found["point_is_certain"] is False
    assert sum("/query" in url for url in opener.urls) == 1
    clipped = _Opener({
        "/data-range": {"first_date": "2025-05-05", "last_date": "2026-11-08"},
        "/query": {"flights": [{"flight_id": "x", "icao24": "abc123", "icao_type": "C172", "path": {"coordinates": [[[-2.9, 35.3, 1000]]]}, "timestamps": [[1_700_000_000]], "alt_correction_ft": []}], "cursor": "more"},
    })
    stopped = execute_covered_plan(
        plan,
        datetime(2026, 11, 7, 22, tzinfo=timezone.utc),
        datetime(2026, 11, 7, 23, tzinfo=timezone.utc),
        _settings(),
        {"status": "accepted", "time_s": [0, 1, 2, 3, 4, 5, 6, 7], "frequency_hz": [180] * 8},
        datetime(2026, 11, 7, 22, 30, tzinfo=timezone.utc).timestamp(),
        0.0,
        None,
        clipped,
    )
    assert stopped["status"] == "incomplete"
    assert stopped["show_point"] is False
    assert stopped["hypotheses"] == []


def test_fitting_province_job_finishes_without_a_network(monkeypatch):
    def fake(*_args, **_kwargs):
        return {"status": "no_matches", "flights": [], "truncated": False, "message": "sin vuelos"}

    monkeypatch.setattr("aerodoppler.history.query_history", fake)
    client = TestClient(create_app(_settings()))
    started = client.post(
        "/api/locate",
        json={
            "region": "melilla",
            "recording_start_local": "2026-11-07T23:00",
            "uncertainty_h": 1,
            "ridge": {"status": "accepted", "time_s": [0, 1, 2, 3, 4, 5, 6, 7], "frequency_hz": [180] * 8},
        },
        headers={"Authorization": "Bearer " + ("x" * 24)},
    )
    assert started.status_code == 200
    body = started.json()
    assert body["mode"] == "province_search"
    assert body["job_id"]
    deadline = time.time() + 8
    result = None
    while time.time() < deadline:
        polled = client.get("/api/locate-jobs/" + body["job_id"], headers={"Authorization": "Bearer " + ("x" * 24)})
        assert polled.status_code == 200
        payload = polled.json()
        if payload["state"] == "done":
            result = payload["result"]
            break
        time.sleep(0.05)
    assert result is not None
    assert result["verdict"] == "failed"
    assert result["show_point"] is False
    assert result["point_is_certain"] is False
    assert result["executed"] is True


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
    province = subprocess.run(
        [sys.executable, "-m", "aerodoppler.wide_search", "--plan", "--provincia", "melilla"],
        check=True,
        capture_output=True,
        text=True,
        cwd="/workspace",
        env={"PYTHONPATH": "/workspace/aerodoppler/src", "PATH": "/usr/bin"},
    )
    melilla = json.loads(province.stdout)
    assert melilla["fits_free_web"] is True
    assert melilla["tile_count"] == 1


class _Opener:
    def __init__(self, routes):
        self.routes = routes
        self.urls = []

    def __call__(self, request, timeout=None):
        self.urls.append(request.full_url)
        for fragment, payload in self.routes.items():
            if fragment in request.full_url:
                return _Response(payload)
        raise AssertionError(request.full_url)


class _Response:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()
        self.headers = {}

    def read(self, limit):
        return self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


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
