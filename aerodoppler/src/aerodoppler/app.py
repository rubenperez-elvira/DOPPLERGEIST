"""Spanish mobile web application. Nothing here is public without a configured token."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .audio_engine import AudioRejected, analyze_samples, decode_audio, ridge_csv, sniff_media
from .auth import COOKIE, LoginLimiter, origin_allowed, request_authorized, session_value, token_matches, unauthorized
from .compare import compare_csv, compare_tracks
from .config import Settings, load_settings
from .history import PROVIDERS, flights_csv, query_history, query_live_adsb_lol

MADRID = ZoneInfo("Europe/Madrid")
log = logging.getLogger("aerodoppler")
WEB = Path(__file__).with_name("web")
LICENSE_PATH = Path(__file__).resolve().parents[2] / "LICENSE"
limiter = LoginLimiter()
analysis_slots = asyncio.Semaphore(1)
history_slots = asyncio.Semaphore(2)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="AeroDoppler v14", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'none'; form-action 'self'"
        response.headers["Cache-Control"] = "no-store"
        return response

    if WEB.is_dir():
        app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "service": "aerodoppler-v14", "version": __version__, "auth_configured": settings.auth_configured}

    @app.get("/readyz")
    def readyz():
        ffmpeg_ok = _binary_ok(os.environ.get("FFMPEG_BIN", "ffmpeg")) and _binary_ok(os.environ.get("FFPROBE_BIN", "ffprobe"))
        ready = settings.auth_configured and ffmpeg_ok
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"ready": ready, "auth_configured": settings.auth_configured, "ffmpeg": ffmpeg_ok},
        )

    @app.get("/license.txt")
    def license_text():
        if LICENSE_PATH.is_file():
            return PlainTextResponse(LICENSE_PATH.read_text(encoding="utf-8"))
        return PlainTextResponse("Required Notice: Copyright 2026 Cinna Davis\nPolyForm Noncommercial 1.0.0\n", status_code=200)

    @app.get("/login", response_class=HTMLResponse)
    def login_page():
        return HTMLResponse((_page("login.html")))

    @app.post("/login")
    async def login(request: Request, access_code: str = Form(default="")):
        if not settings.auth_configured:
            return unauthorized("El acceso no está configurado. El servicio permanece cerrado.", "auth_not_configured", 503)
        if not origin_allowed(request):
            return unauthorized("El formulario no viene de este sitio.", "origin", 403)
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            return unauthorized("Demasiados intentos. Espera unos minutos.", "rate_limit", 429)
        if not token_matches(access_code.strip(), settings.access_token or ""):
            return unauthorized("El código de acceso no es válido.")
        response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE,
            session_value(settings.access_token or ""),
            httponly=True,
            secure=settings.cookie_secure or request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
            samesite="lax",
            max_age=12 * 60 * 60,
            path="/",
        )
        return response

    @app.post("/logout")
    def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if not settings.auth_configured:
            return HTMLResponse(_page("closed.html"), status_code=503)
        if not request_authorized(request, settings):
            return HTMLResponse(_page("login.html"))
        return HTMLResponse(_page("index.html"))

    @app.get("/api/providers")
    def providers(request: Request):
        blocked = _guard(request, settings)
        if blocked:
            return blocked
        return {"providers": PROVIDERS, "default": "adsb_aero"}

    @app.post("/api/analyze")
    async def analyze(request: Request, file: UploadFile = File(...)):
        blocked = _guard(request, settings, browser=True)
        if blocked:
            return blocked
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_upload_bytes + 8192:
            return _error(413, "archivo_grande", f"El archivo supera {settings.max_upload_bytes // (1024 * 1024)} MiB.")
        data = await file.read(settings.max_upload_bytes + 1)
        await file.close()
        if len(data) > settings.max_upload_bytes:
            return _error(413, "archivo_grande", f"El archivo supera {settings.max_upload_bytes // (1024 * 1024)} MiB.")
        if sniff_media(data) is None:
            return _error(415, "tipo_no_admitido", "Solo se aceptan WAV, MP3, M4A y MP4 con audio.")
        if not await _acquire(analysis_slots):
            return _busy()
        try:
            try:
                result = await asyncio.wait_for(asyncio.to_thread(_analyze_bytes, data, settings), timeout=settings.analysis_timeout_s)
            except TimeoutError:
                return _error(408, "tiempo_agotado", "El análisis superó el tiempo permitido. Prueba con un fragmento más corto.")
            except AudioRejected as error:
                return _error(422, error.code, error.message)
        finally:
            analysis_slots.release()
        result["csv"] = ridge_csv(result)
        result["stored"] = False
        return result

    @app.post("/api/history")
    async def history(request: Request):
        blocked = _guard(request, settings, browser=True)
        if blocked:
            return blocked
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return _error(400, "json", "El cuerpo no es JSON.")
        if not isinstance(payload, dict):
            return _error(400, "json", "La consulta tiene que ser un objeto JSON.")
        try:
            lat, lon, place_note = await asyncio.to_thread(_resolve_place, payload)
            center = _parse_local(str(payload.get("local_datetime", "")))
            half = float(payload["window_min"])
            radius = float(payload["radius_km"])
        except (KeyError, TypeError, ValueError) as error:
            return _error(422, "consulta", str(error) or "Parámetros de consulta no válidos.")
        start, end = center - timedelta(minutes=half), center + timedelta(minutes=half)
        provider = str(payload.get("provider") or "adsb_aero")
        key = os.environ.get("AERDOPPLER_FLYITALY_API_KEY", "").strip() or None
        if not await _acquire(history_slots):
            return _busy()
        try:
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(query_history, provider, lat, lon, radius, start, end, settings, None, key),
                    timeout=settings.history_timeout_s * settings.max_history_pages + 10,
                )
            except TimeoutError:
                return _error(504, "api_error", "La consulta histórica superó el tiempo permitido.")
            except Exception as error:
                from .history import HistoryError

                if isinstance(error, HistoryError):
                    return _error(422, error.status, error.message)
                return _error(502, "api_error", "Fallo al consultar el proveedor.")
        finally:
            history_slots.release()
        result["place_note"] = place_note
        result["center_local"] = center.astimezone(MADRID).isoformat()
        result["csv"] = flights_csv(result)
        return result

    @app.post("/api/live")
    async def live(request: Request):
        blocked = _guard(request, settings, browser=True)
        if blocked:
            return blocked
        try:
            payload = await request.json()
            lat, lon, _note = await asyncio.to_thread(_resolve_place, payload)
            radius_nm = int(payload.get("radius_nm") or 25)
        except (KeyError, TypeError, ValueError) as error:
            return _error(422, "consulta", str(error) or "Parámetros en vivo no válidos.")
        try:
            result = await asyncio.wait_for(asyncio.to_thread(query_live_adsb_lol, lat, lon, radius_nm), timeout=25)
        except TimeoutError:
            return _error(504, "api_error", "La consulta en vivo superó el tiempo permitido.")
        return result

    @app.post("/api/elevation")
    async def elevation(request: Request):
        blocked = _guard(request, settings, browser=True)
        if blocked:
            return blocked
        try:
            payload = await request.json()
            lat = float(payload["lat"])
            lon = float(payload["lon"])
        except (KeyError, TypeError, ValueError):
            return _error(422, "consulta", "Hacen falta latitud y longitud.")
        try:
            value = await asyncio.to_thread(_elevation, lat, lon)
        except Exception:
            return _error(502, "elevacion", "No se pudo consultar la elevación del terreno. Indica la altitud a mano.")
        return {
            "alt_m": value,
            "source": "https://api.open-meteo.com/v1/elevation",
            "note": "Elevación del terreno en metros, no es la altura del micrófono ni una cota elipsoidal verificada.",
        }

    @app.post("/api/compare")
    async def compare(request: Request):
        blocked = _guard(request, settings, browser=True)
        if blocked:
            return blocked
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > 1_500_000:
            return _error(413, "consulta", "La comparación enviada es demasiado grande.")
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return _error(400, "json", "El cuerpo no es JSON.")
        try:
            result = await asyncio.wait_for(asyncio.to_thread(_compare_payload, payload), timeout=settings.analysis_timeout_s)
        except TimeoutError:
            return _error(408, "tiempo_agotado", "La comparación superó el tiempo permitido.")
        except ValueError as error:
            return _error(422, "comparacion", str(error))
        result["csv"] = compare_csv(result)
        result["stored"] = False
        return result

    @app.exception_handler(Exception)
    async def unexpected(_request: Request, _exc: Exception):
        log.exception("petición fallida")
        return _error(500, "interno", "Error interno. El audio temporal, si existía, se ha descartado.")

    return app


def _guard(request: Request, settings: Settings, browser: bool = False):
    if not settings.auth_configured:
        return unauthorized("El acceso no está configurado. El servicio permanece cerrado.", "auth_not_configured", 503)
    if browser and request.cookies.get(COOKIE) and not origin_allowed(request):
        return unauthorized("La petición del navegador no coincide con este sitio.", "origin", 403)
    if "access_token" in request.query_params or "code" in request.query_params:
        return unauthorized("No envíes el código en la dirección.", "secret_in_url", 400)
    if not request_authorized(request, settings):
        return unauthorized()
    return None


async def _acquire(slot: asyncio.Semaphore) -> bool:
    try:
        await asyncio.wait_for(slot.acquire(), timeout=0.05)
    except TimeoutError:
        return False
    return True


def _busy():
    return _error(429, "ocupado", "Ya hay un análisis en curso. En Render Free solo cabe uno a la vez.")


def _error(status: int, code: str, message: str):
    return JSONResponse(status_code=status, content={"error": code, "message": message})


def _page(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def _binary_ok(name: str) -> bool:
    from shutil import which

    return which(name) is not None


def _analyze_bytes(data: bytes, settings: Settings) -> dict:
    with tempfile.TemporaryDirectory(prefix="adtmp-") as temporary:
        path = Path(temporary) / "input.bin"
        path.write_bytes(data)
        samples = decode_audio(
            os.environ.get("FFMPEG_BIN", "ffmpeg"),
            os.environ.get("FFPROBE_BIN", "ffprobe"),
            path,
            settings.sample_rate,
            settings.max_duration_s,
            settings.ffmpeg_timeout_s,
        )
    return analyze_samples(samples, settings.sample_rate)


def _parse_local(value: str) -> datetime:
    text = value.strip()
    if not text:
        raise ValueError("Falta la fecha y hora local.")
    if text.endswith("Z"):
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    else:
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MADRID)
    return parsed.astimezone(timezone.utc)


def _resolve_place(payload: dict):
    lat, lon = payload.get("lat"), payload.get("lon")
    if lat not in (None, "") and lon not in (None, ""):
        return float(lat), float(lon), "Coordenadas indicadas por el usuario."
    place = str(payload.get("place") or "").strip()
    if len(place) < 2 or len(place) > 80:
        raise ValueError("Escribe un lugar o unas coordenadas.")
    url = "https://geocoding-api.open-meteo.com/v1/search?count=5&language=es&format=json&name=" + quote(place)
    request = Request(url, headers={"User-Agent": "AeroDoppler-v14/14.0.0", "Accept": "application/json"})
    with urlopen(request, timeout=15) as response:
        data = json.loads(response.read(200_000).decode())
    results = data.get("results") or []
    if not results:
        raise ValueError("Ese lugar no tuvo resultados. Usa coordenadas.")
    choice = payload.get("place_index")
    if choice is None:
        if len(results) > 1:
            names = [f"{item.get('name')} ({item.get('admin1')}, {item.get('country')})" for item in results[:5]]
            raise ValueError("Lugar ambiguo: " + "; ".join(names))
        choice = 0
    item = results[int(choice)]
    return float(item["latitude"]), float(item["longitude"]), f"{item.get('name')}, {item.get('admin1')}, {item.get('country')} (Open-Meteo)"


def _elevation(lat: float, lon: float) -> float:
    url = f"https://api.open-meteo.com/v1/elevation?latitude={lat:.6f}&longitude={lon:.6f}"
    request = Request(url, headers={"User-Agent": "AeroDoppler-v14/14.0.0", "Accept": "application/json"})
    with urlopen(request, timeout=15) as response:
        data = json.loads(response.read(100_000).decode())
    value = float(data["elevation"][0])
    if not -500 <= value <= 9000:
        raise ValueError("elevación fuera de rango")
    return value


def _compare_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("La comparación tiene que ser un objeto.")
    observer = payload.get("observer") or {}
    flights = payload.get("flights") or []
    if not isinstance(flights, list) or len(flights) > 40:
        raise ValueError("Hay que enviar entre 0 y 40 trayectorias.")
    ridge = payload.get("ridge")
    if ridge is not None:
        if not isinstance(ridge, dict) or len(ridge.get("time_s") or []) > 4000:
            raise ValueError("La pista de audio no es válida.")
        if len(ridge.get("time_s") or []) != len(ridge.get("frequency_hz") or []):
            raise ValueError("La pista de audio está incompleta.")
    recording = payload.get("recording_start_local")
    recording_unix = None if not recording else _parse_local(str(recording)).timestamp()
    f0 = payload.get("f0_hz")
    f0_value = None if f0 in (None, "") else float(f0)
    if observer.get("alt_m") in (None, "") and observer.get("lookup_elevation"):
        observer = dict(observer)
        observer["alt_m"] = _elevation(float(observer["lat"]), float(observer["lon"]))
        observer["altitude_note"] = "Elevación Open-Meteo del terreno, no una cota de micrófono."
    cleaned = []
    for flight in flights:
        points = flight.get("points") or []
        if len(points) > 240:
            raise ValueError("Una trayectoria trae demasiados puntos.")
        cleaned.append(flight)
    window = None
    if payload.get("window_start_unix") not in (None, "") and payload.get("window_end_unix") not in (None, ""):
        window = (float(payload["window_start_unix"]), float(payload["window_end_unix"]))
        if window[1] <= window[0]:
            raise ValueError("La ventana de comparación está invertida.")
    return compare_tracks(observer, cleaned, ridge, recording_unix, f0_value, window=window)


app = create_app()
