import os
from pathlib import Path

from fastapi.testclient import TestClient

from aerodoppler.app import _parse_local, create_app
from aerodoppler.config import Settings

TOKEN = "prueba-codigo-acceso-v14-seguro"


def _settings(**changes):
    values = dict(
        access_token=TOKEN,
        cookie_secure=False,
        max_upload_bytes=200_000,
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
    values.update(changes)
    return Settings(**values)


def _client(settings=None):
    return TestClient(create_app(settings or _settings()))


def test_closed_without_token_and_secret_not_in_pages():
    client = _client(_settings(access_token=None))
    page = client.get("/")
    assert page.status_code == 503
    assert "cerrado" in page.text.lower()
    assert TOKEN not in page.text
    denied = client.post("/api/analyze", files={"file": ("a.wav", b"RIFF1234WAVEdata", "audio/wav")})
    assert denied.status_code == 503
    assert "AERDOPPLER_ACCESS_TOKEN" not in denied.text
    license_page = client.get("/license.txt")
    assert "PolyForm Noncommercial" in license_page.text
    assert "Copyright 2026 Cinna Davis" in license_page.text


def test_login_cookie_and_limits(tmp_path: Path):
    client = _client()
    assert client.post("/login", data={"access_code": TOKEN}).status_code == 403
    bad = client.post("/login", data={"access_code": "clave-equivocada-pero-larga"}, headers={"Origin": "http://testserver"})
    assert bad.status_code == 401
    good = client.post("/login", data={"access_code": TOKEN}, headers={"Origin": "http://testserver"})
    assert good.status_code == 200
    assert client.cookies.get("ad_session")
    assert TOKEN not in good.headers.get("set-cookie", "")
    home = client.get("/")
    assert "Analizar audio" in home.text
    health = client.get("/healthz")
    assert health.json()["auth_configured"] is True
    oversized = b"RIFF" + b"xxxx" + b"WAVE" + b"\x00" * 250_000
    rejected = client.post("/api/analyze", files={"file": ("grande.wav", oversized, "audio/wav")}, headers={"Origin": "http://testserver"})
    assert rejected.status_code == 413
    query = client.post(
        "/api/history",
        json={"lat": 40.97786, "lon": -5.60845, "local_datetime": "2026-11-07T23:30", "window_min": 30, "radius_km": 400, "provider": "adsb_aero"},
        headers={"Origin": "http://testserver"},
    )
    assert query.status_code == 422
    leaked = client.get("/api/providers?access_token=" + TOKEN, headers={"Origin": "http://testserver"})
    assert leaked.status_code == 400


def test_bearer_history_status_and_madrid_clock():
    moment = _parse_local("2026-11-07T23:30")
    assert moment.isoformat() == "2026-11-07T22:30:00+00:00"
    client = _client()
    response = client.post(
        "/api/history",
        json={"lat": 40.97786, "lon": -5.60845, "local_datetime": "2026-11-07T23:30", "window_min": 30, "radius_km": 80, "provider": "adsb_lol_live"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unsupported_history"
    assert body["requested"]["start_utc"].startswith("2026-11-07T22:00:00")
    assert body["requested"]["end_utc"].startswith("2026-11-07T23:00:00")
    assert TOKEN not in response.text
    assert "AERDOPPLER_ACCESS_TOKEN" not in os.environ.get("AERDOPPLER_ACCESS_TOKEN", response.text)


def test_short_token_is_not_configured():
    client = _client(_settings(access_token=None))
    assert client.get("/readyz").status_code == 503
