import json
from datetime import datetime, timezone
from io import BytesIO
from urllib.error import HTTPError

from aerodoppler.config import Settings
from aerodoppler.history import classify_aircraft, query_history, query_live_adsb_lol


def settings():
    return Settings(
        access_token="x" * 24,
        cookie_secure=False,
        max_upload_bytes=1024 * 1024,
        max_duration_s=90,
        sample_rate=16000,
        max_radius_km=120,
        max_window_min=180,
        ffmpeg_timeout_s=20,
        history_timeout_s=20,
        max_history_pages=2,
        max_flights=10,
        lookback_h=72,
        analysis_timeout_s=30,
    )


class Response:
    def __init__(self, payload, headers=None):
        self._body = json.dumps(payload).encode()
        self.headers = headers or {}

    def read(self, limit):
        return self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class Opener:
    def __init__(self, routes):
        self.routes = routes
        self.urls = []

    def __call__(self, request, timeout=None):
        self.urls.append(request.full_url)
        for fragment, payload in self.routes.items():
            if fragment in request.full_url:
                if isinstance(payload, Exception):
                    raise payload
                return Response(payload)
        raise AssertionError(request.full_url)


def test_live_provider_is_not_history_and_makes_no_request():
    opener = Opener({})
    result = query_history(
        "adsb_lol_live", 40.97786, -5.60845, 80, _start(), _end(), settings(), opener
    )
    assert result["status"] == "unsupported_history"
    assert result["live_used_as_history"] is False
    assert result["zero_flights_claim"] is False
    assert opener.urls == []


def test_archive_provider_is_not_a_fake_query():
    result = query_history("adsblol_globe_archive", 40.97786, -5.60845, 80, _start(), _end(), settings(), Opener({}))
    assert result["status"] == "unsupported_query"
    assert "2000000000" in result["message"]
    assert result["flights"] == []


def test_flyitaly_without_key_is_auth_required():
    opener = Opener({})
    result = query_history("flyitaly", 40.97786, -5.60845, 80, _start(), _end(), settings(), opener, None)
    assert result["status"] == "auth_required"
    assert result["flights"] is None
    assert opener.urls == []


def test_outside_archive_is_unknown_coverage_not_zero_flights():
    opener = Opener({"/data-range": {"first_date": "2025-05-05", "last_date": "2026-11-08"}})
    old = datetime(2024, 11, 7, 22, 0, tzinfo=timezone.utc)
    result = query_history("adsb_aero", 40.9, -5.6, 20, old, old.replace(hour=23), settings(), opener)
    assert result["status"] == "unknown_coverage"
    assert result["flights"] is None
    assert result["zero_flights_claim"] is False
    assert all("query" not in url for url in opener.urls)


def test_completed_empty_response_is_not_a_coverage_claim():
    opener = Opener({
        "/data-range": {"first_date": "2025-05-05", "last_date": "2026-11-08"},
        "/query": {"flights": [], "cursor": None, "window_from": "2026-11-05T00:00:00Z"},
    })
    result = query_history("adsb_aero", 40.97786, -5.60845, 80, _start(), _end(), settings(), opener)
    assert result["status"] == "no_matches"
    assert result["provider_returned_empty"] is True
    assert result["zero_flights_claim"] is False
    assert "no demuestra" in result["message"]


def test_provider_http_error_is_not_zero_flights():
    error = HTTPError("https://adsb.aero/api/v1/data-range", 503, "down", hdrs=None, fp=BytesIO(b'{"hint":"later"}'))
    opener = Opener({"/data-range": error})
    result = query_history("adsb_aero", 40.97786, -5.60845, 80, _start(), _end(), settings(), opener)
    assert result["status"] == "api_error"
    assert result["flights"] is None


def test_real_shape_is_compacted_and_jets_are_marked():
    flight = {
        "flight_id": "abc123:2026-11-07T22:00:00Z",
        "icao24": "abc123",
        "callsign": "TEST1",
        "icao_type": "B738",
        "emitter_category": "A3",
        "registration": "EC-TEST",
        "model": "BOEING 737-800",
        "start_ts": "2026-11-07T21:00:00Z",
        "end_ts": "2026-11-07T23:30:00Z",
        "path": {"coordinates": [[[-5.7, 41.1, 35000.0], [-5.5, 40.9, 35100.0]]]},
        "timestamps": [[_start().timestamp() + 60, _start().timestamp() + 600]],
        "alt_correction_ft": [[[_start().timestamp(), 100.0]]],
    }
    opener = Opener({
        "/data-range": {"first_date": "2025-05-05", "last_date": "2026-11-08"},
        "/query": {"flights": [flight], "cursor": None, "window_from": "2026-11-05T00:00:00Z"},
    })
    result = query_history("adsb_aero", 40.97786, -5.60845, 80, _start(), _end(), settings(), opener)
    assert result["status"] == "ok"
    assert result["count"] == 1
    row = result["flights"][0]
    assert row["engine_class"] == "jet"
    assert row["acoustic_eligible"] is False
    assert row["points"][0]["altitude_basis"] == "pressure_plus_qnh_correction_ft"
    assert row["points"][0]["alt_m"] == (35000.0 + 100.0) * 0.3048
    assert classify_aircraft("C172", None)["acoustic_eligible"] is True


def test_live_query_marks_itself_as_live():
    opener = Opener({"/v2/point/": {"ac": [{"hex": "abc123", "flight": "NOW1", "lat": 41.0, "lon": -5.6}], "now": 10, "total": 1}})
    result = query_live_adsb_lol(40.97786, -5.60845, 43, opener)
    assert result["is_history"] is False
    assert result["live"] is True
    assert result["count"] == 1


def _start():
    return datetime(2026, 11, 7, 22, 0, tzinfo=timezone.utc)


def _end():
    return datetime(2026, 11, 7, 23, 0, tzinfo=timezone.utc)
