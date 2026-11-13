"""Historical flight providers.

Live traffic is never returned as history. An empty provider response is not a
claim that no aircraft flew, and a network or authentication failure is not an
empty sky. adsb.aero is queried for real history. ADSB.lol's public API is live
only. FlyItalyADSB documents history but requires a key that is not assumed.
The global ADSB.lol tarball has no bounded query and is not downloaded here.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .physics import FT_TO_M

USER_AGENT = "AeroDoppler-v14/14.0.0 (noncommercial personal research; +https://github.com/1337isnot1337/DOPPLERGEIST)"
ADSB_AERO = "https://adsb.aero/api/v1"
ADSB_LOL = "https://api.adsb.lol"

JET_TYPES = {
    "A306", "A30B", "A310", "A318", "A319", "A320", "A20N", "A321", "A21N", "A332", "A333", "A338", "A339",
    "A342", "A343", "A345", "A346", "A359", "A35K", "A388", "A3ST", "B731", "B732", "B733", "B734", "B735",
    "B736", "B737", "B738", "B739", "B37M", "B38M", "B39M", "B3XM", "B741", "B742", "B743", "B744", "B748",
    "B752", "B753", "B762", "B763", "B764", "B772", "B773", "B77L", "B77W", "B788", "B789", "B78X", "E170",
    "E190", "E195", "E75L", "E75S", "E290", "E295", "CRJ2", "CRJ7", "CRJ9", "CRJX", "BCS1", "BCS3", "MD11",
    "MD81", "MD82", "MD83", "DC10", "C25A", "C25B", "C25C", "C510", "C525", "C56X", "C680", "C68A", "C750",
    "GLF4", "GLF5", "GLF6", "GLEX", "GL5T", "GL7T", "LJ35", "LJ45", "LJ60", "F900", "FA7X", "FA8X", "F2TH",
}
TURBOPROP_TYPES = {"AT43", "AT45", "AT72", "AT75", "AT76", "DH8A", "DH8B", "DH8C", "DH8D", "C208", "PC12", "B350", "BE20", "TBM7", "TBM8", "TBM9", "SF34", "SB20"}
PISTON_TYPES = {"C152", "C172", "C182", "C206", "C210", "P28A", "P28R", "PA32", "PA34", "PA46", "SR20", "SR22", "DA40", "DA42"}
ROTOR_TYPES = {"R22", "R44", "R66", "B06", "B407", "B412", "EC35", "EC45", "A139", "S92", "AS50", "AS65"}

PROVIDERS = {
    "adsb_aero": {
        "label": "adsb.aero (histórico)",
        "historical": True,
        "live": False,
        "verified": True,
        "docs": "https://adsb.aero/llms.txt",
        "license": "Datos derivados del archivo adsb.lol, ODbL 1.0. El software del servicio es un proyecto distinto. Cobertura desigual.",
        "archive": "https://github.com/adsblol/globe_history_2026",
    },
    "adsb_lol_live": {
        "label": "ADSB.lol (solo en vivo)",
        "historical": False,
        "live": True,
        "verified": True,
        "docs": "https://api.adsb.lol/docs",
        "license": "ODbL 1.0. La especificación pública no incluye un endpoint histórico.",
    },
    "flyitaly": {
        "label": "FlyItalyADSB (histórico con clave)",
        "historical": True,
        "live": True,
        "verified": True,
        "requires_key": True,
        "docs": "https://flyitalyadsb.com/en/data/api/docs/history/",
        "license": "CC BY-SA 4.0 según su documentación. El plan gratuito documentado limita el radio a 10 km.",
    },
    "adsblol_globe_archive": {
        "label": "Archivo diario ADSB.lol (sin API acotada)",
        "historical": False,
        "live": False,
        "verified": True,
        "docs": "https://github.com/adsblol/globe_history_2026",
        "license": "ODbL 1.0. Publicación diaria global, no una consulta por lugar y hora.",
    },
}


class HistoryError(Exception):
    def __init__(self, status: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def classify_aircraft(icao_type: str | None, emitter: str | None) -> dict:
    code = (icao_type or "").upper()
    if code in JET_TYPES:
        kind, eligible = "jet", False
    elif code in TURBOPROP_TYPES:
        kind, eligible = "turboprop", True
    elif code in PISTON_TYPES:
        kind, eligible = "piston", True
    elif code in ROTOR_TYPES or (emitter or "").upper() == "A7":
        kind, eligible = "rotorcraft", True
    else:
        kind, eligible = "unknown", False
    return {
        "engine_class": kind,
        "acoustic_eligible": eligible,
        "policy": "El ranking acústico usa solo tipos no reactores confirmados. El tipo desconocido queda fuera.",
    }


def query_history(provider: str, lat: float, lon: float, radius_km: float, start: datetime, end: datetime, settings, opener=None, flyitaly_key: str | None = None) -> dict:
    if start.tzinfo is None or end.tzinfo is None:
        raise HistoryError("unsupported_query", "El intervalo tiene que llevar zona horaria.")
    if end <= start:
        raise HistoryError("unsupported_query", "El fin del intervalo tiene que ser posterior al inicio.")
    window_min = (end - start).total_seconds() / 60.0
    if window_min > settings.max_window_min or radius_km <= 0 or radius_km > settings.max_radius_km:
        raise HistoryError(
            "unsupported_query",
            f"Radio máximo {settings.max_radius_km:.0f} km y ventana máxima {settings.max_window_min:.0f} min.",
        )
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise HistoryError("unsupported_query", "Las coordenadas están fuera de rango.")
    opener = opener or urlopen
    common = {
        "provider": provider,
        "live_used_as_history": False,
        "zero_flights_claim": False,
        "coverage": "unknown",
        "requested": {
            "lat": lat,
            "lon": lon,
            "radius_km": radius_km,
            "start_utc": start.astimezone(timezone.utc).isoformat(),
            "end_utc": end.astimezone(timezone.utc).isoformat(),
        },
    }
    if provider == "adsb_lol_live":
        return {
            **common,
            "status": "unsupported_history",
            "flights": [],
            "message": "ADSB.lol expone tráfico en vivo. Su API pública no tiene un endpoint histórico. En vivo no es historia.",
            "docs": PROVIDERS[provider]["docs"],
        }
    if provider == "adsblol_globe_archive":
        return {
            **common,
            "status": "unsupported_query",
            "flights": [],
            "message": (
                "El archivo globe_history_2026 es una publicación diaria global bajo ODbL, "
                "no una consulta por radio. El día 2026-11-07 prod pesa 2000000000 + 1404765184 bytes. "
                "No cabe ni se descarga en Render Free, y esta respuesta no es una búsqueda ejecutada."
            ),
            "docs": PROVIDERS[provider]["docs"],
            "release": "https://github.com/adsblol/globe_history_2026/releases/tag/v2026.11.07-planes-readsb-prod-0",
        }
    if provider == "flyitaly":
        return _flyitaly(common, lat, lon, radius_km, start, end, settings, opener, flyitaly_key)
    if provider == "adsb_aero":
        return _adsb_aero(common, lat, lon, radius_km, start, end, settings, opener)
    raise HistoryError("unsupported_query", "Proveedor no reconocido.")


def query_live_adsb_lol(lat: float, lon: float, radius_nm: int, opener=None, timeout: float = 20.0) -> dict:
    if not 1 <= radius_nm <= 250:
        raise HistoryError("unsupported_query", "El radio en vivo de ADSB.lol admite de 1 a 250 millas náuticas.")
    url = f"{ADSB_LOL}/v2/point/{lat:.5f}/{lon:.5f}/{int(radius_nm)}"
    try:
        payload = _get_json(url, timeout, opener or urlopen)
    except HistoryError as error:
        return {"status": "api_error", "live": True, "is_history": False, "message": error.message, "flights": None}
    aircraft = payload.get("ac") if isinstance(payload, dict) else None
    if not isinstance(aircraft, list):
        return {"status": "api_error", "live": True, "is_history": False, "message": "La respuesta en vivo no trae una lista de aeronaves.", "flights": None}
    rows = []
    for item in aircraft[:80]:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "icao24": item.get("hex"),
                "callsign": (item.get("flight") or "").strip() or None,
                "lat": item.get("lat"),
                "lon": item.get("lon"),
                "type": item.get("t"),
                "seen_pos_s": item.get("seen_pos"),
            }
        )
    return {
        "status": "ok",
        "live": True,
        "is_history": False,
        "message": "Tráfico recibido ahora mismo por ADSB.lol. No describe el 7 de noviembre ni ninguna otra hora pasada.",
        "observed_at_ms": payload.get("now"),
        "count": len(rows),
        "flights": rows,
        "license": "ODbL 1.0",
    }


def _flyitaly(common, lat, lon, radius_km, start, end, settings, opener, key):
    docs = PROVIDERS["flyitaly"]["docs"]
    if not key:
        return {
            **common,
            "status": "auth_required",
            "flights": None,
            "message": (
                "FlyItalyADSB documenta historia y /v2/overflights, pero exige X-Api-Key. "
                "No hay clave en el entorno. El plan gratuito documentado admite 10 km, así que 80 km lo excede. "
                "No se ha llamado a la API y esto no es cero vuelos."
            ),
            "docs": docs,
        }
    if radius_km > 50:
        return {
            **common,
            "status": "plan_limit",
            "flights": None,
            "message": "La documentación de FlyItaly limita /overflights a 50 km como máximo, y a 10 km en el plan gratuito.",
            "docs": docs,
        }
    query = (
        f"https://api.flyitalyadsb.com/v2/overflights?lat={lat:.6f}&lon={lon:.6f}&dist={radius_km:.3f}"
        f"&from={start.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&to={end.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    try:
        payload, headers = _get_json(query, settings.history_timeout_s, opener, headers={"X-Api-Key": key}, return_headers=True)
    except HistoryError as error:
        status = "plan_limit" if error.status == "http_403" else "api_error"
        if error.status == "http_401":
            status = "auth_required"
        return {**common, "status": status, "flights": None, "message": error.message, "docs": docs}
    rows = payload if isinstance(payload, list) else payload.get("aircraft") or payload.get("overflights") or []
    if not isinstance(rows, list):
        return {**common, "status": "api_error", "flights": None, "message": "Respuesta de FlyItaly no reconocida.", "docs": docs}
    return {
        **common,
        "status": "no_matches" if not rows else "unparsed_provider_response",
        "provider_returned_empty": not rows,
        "trajectories_parsed": False,
        "flights": None,
        "message": (
            "FlyItaly respondió. Este servicio no convierte todavía su JSON a trayectorias, "
            "así que no hay lista evaluable ni un cero interpretado."
            if rows
            else "FlyItaly completó la consulta y devolvió una lista vacía. La cobertura no queda demostrada."
        ),
        "raw_count": len(rows),
        "docs": docs,
        "response_source": headers.get("X-History-Source"),
    }


def _adsb_aero(common, lat, lon, radius_km, start, end, settings, opener):
    docs = PROVIDERS["adsb_aero"]["docs"]
    try:
        span = _get_json(f"{ADSB_AERO}/data-range", settings.history_timeout_s, opener)
    except HistoryError as error:
        return {**common, "status": "api_error", "flights": None, "message": f"No se pudo leer el rango de archivo de adsb.aero: {error.message}", "docs": docs}
    first, last = span.get("first_date"), span.get("last_date")
    day = start.astimezone(timezone.utc).date().isoformat()
    end_day = (end.astimezone(timezone.utc) - timedelta(seconds=1)).date().isoformat()
    if not first or not last or day < first or end_day > last:
        return {
            **common,
            "status": "unknown_coverage",
            "coverage": "outside_declared_archive",
            "flights": None,
            "archive_range": {"first_date": first, "last_date": last},
            "message": "La fecha pedida queda fuera del archivo que adsb.aero declara. No es un resultado de cero vuelos.",
            "docs": docs,
        }
    body = {
        "match": {
            "trajectory_intersects": {
                "geometry": {"type": "Circle", "coordinates": [lon, lat], "radius": int(round(radius_km * 1000))},
                "time_from": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "time_to": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        },
        "start_from": (start.astimezone(timezone.utc) - timedelta(hours=settings.lookback_h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_date": (end.astimezone(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": 4,
        "limit": min(25, settings.max_flights),
        "include_path": True,
    }
    flights = []
    cursor = None
    pages = 0
    truncated = False
    window_from = None
    try:
        while pages < settings.max_history_pages and len(flights) < settings.max_flights:
            request = dict(body)
            if cursor:
                request["cursor"] = cursor
            payload = _post_json(f"{ADSB_AERO}/query", request, settings.history_timeout_s, opener)
            pages += 1
            window_from = payload.get("window_from")
            batch = payload.get("flights")
            if not isinstance(batch, list):
                return {**common, "status": "api_error", "flights": None, "message": "adsb.aero no devolvió una lista de vuelos.", "docs": docs}
            for item in batch:
                if len(flights) >= settings.max_flights:
                    truncated = True
                    break
                flights.append(_compact_flight(item, lat, lon, start, end, radius_km))
            cursor = payload.get("cursor")
            if not cursor:
                break
        else:
            if cursor:
                truncated = True
    except HistoryError as error:
        status = "unsupported_query" if error.status == "http_422" else "api_error"
        return {**common, "status": status, "flights": None, "message": error.message, "docs": docs, "pages_completed": pages}
    if truncated:
        status = "partial"
        message = "Consulta histórica real, incompleta por el límite de páginas o de vuelos de este servicio."
    elif not flights:
        status = "no_matches"
        message = (
            "adsb.aero completó la consulta y no devolvió vuelos en estos límites. "
            "Eso no demuestra que no hubiera aviones: la cobertura de receptores sigue siendo desconocida "
            f"y solo se buscan salidas desde {settings.lookback_h:.0f} h antes."
        )
    else:
        status = "ok"
        message = "Consulta histórica real en adsb.aero. La cobertura de receptores no está garantizada."
    return {
        **common,
        "status": status,
        "coverage": "declared_archive_includes_date_receiver_coverage_unknown",
        "provider_returned_empty": not flights,
        "flights": flights,
        "count": len(flights),
        "pages": pages,
        "truncated": truncated,
        "window_from": window_from,
        "lookback_h": settings.lookback_h,
        "archive_range": {"first_date": first, "last_date": last},
        "message": message,
        "license": "ODbL 1.0 vía archivo adsb.lol, consultado en adsb.aero",
        "docs": docs,
        "attribution": "Datos de receptores de adsb.lol, redistribuidos por adsb.aero. Open Database License.",
        "path_note": "Las trayectorias están simplificadas. Un vuelo puede figurar por un tramo interpolado aunque sus vértices queden fuera del radio.",
    }


def _compact_flight(item: dict, lat: float, lon: float, start: datetime, end: datetime, radius_km: float) -> dict:
    points = []
    coordinates = (item.get("path") or {}).get("coordinates") or []
    stamps = item.get("timestamps") or []
    corrections = item.get("alt_correction_ft") or []
    for segment, times in zip(coordinates, stamps):
        for coordinate, stamp in zip(segment, times):
            if len(coordinate) < 3:
                continue
            point_lon, point_lat, pressure_ft = float(coordinate[0]), float(coordinate[1]), float(coordinate[2])
            correction = _step_value(corrections, float(stamp))
            if correction is None:
                altitude_m = pressure_ft * FT_TO_M
                basis = "pressure_altitude_ft"
            else:
                altitude_m = (pressure_ft + correction) * FT_TO_M
                basis = "pressure_plus_qnh_correction_ft"
            points.append(
                {
                    "t": float(stamp),
                    "lat": point_lat,
                    "lon": point_lon,
                    "alt_m": altitude_m,
                    "altitude_basis": basis,
                }
            )
    start_s = start.astimezone(timezone.utc).timestamp()
    end_s = end.astimezone(timezone.utc).timestamp()
    inside = [point for point in points if start_s <= point["t"] <= end_s]
    nearest = None
    for point in inside or points:
        ground = _haversine_m(lat, lon, point["lat"], point["lon"])
        if nearest is None or ground < nearest["ground_m"]:
            nearest = {"ground_m": ground, "t": point["t"], "lat": point["lat"], "lon": point["lon"], "alt_m": point["alt_m"]}
    kept = _downsample(points, 180)
    classification = classify_aircraft(item.get("icao_type"), item.get("emitter_category"))
    return {
        "flight_id": item.get("flight_id"),
        "icao24": item.get("icao24"),
        "callsign": item.get("callsign"),
        "registration": item.get("registration"),
        "icao_type": item.get("icao_type"),
        "model": item.get("model"),
        "emitter_category": item.get("emitter_category"),
        "start_ts": item.get("start_ts"),
        "end_ts": item.get("end_ts"),
        **classification,
        "points": kept,
        "point_count_returned": len(kept),
        "nearest_vertex_in_window": nearest,
        "vertices_outside_requested_radius": bool(nearest and nearest["ground_m"] > radius_km * 1000.0),
        "identification": False,
    }


def _step_value(series, instant: float):
    value = None
    best = None
    for segment in series or []:
        if not isinstance(segment, list):
            continue
        for pair in segment:
            if not isinstance(pair, list) or len(pair) < 2 or pair[1] is None:
                continue
            stamp = float(pair[0])
            if stamp <= instant and (best is None or stamp >= best):
                best = stamp
                value = float(pair[1])
    return value


def _downsample(points: list[dict], limit: int) -> list[dict]:
    if len(points) <= limit:
        return points
    if limit < 2:
        return points[:1]
    step = (len(points) - 1) / (limit - 1)
    chosen = [points[int(round(index * step))] for index in range(limit)]
    return chosen


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def _request(url: str, timeout: float, opener, data=None, headers=None):
    head = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        head.update(headers)
    if data is not None:
        head["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=head)
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read(2_000_000)
            if len(body) >= 2_000_000:
                raise HistoryError("api_error", "La respuesta del proveedor supera el tamaño admitido.")
            text = body.decode("utf-8", "replace")
            try:
                payload = json.loads(text) if text else {}
            except json.JSONDecodeError as error:
                raise HistoryError("api_error", "El proveedor no devolvió JSON.") from error
            return payload, {key: value for key, value in response.headers.items()}
    except HistoryError:
        raise
    except HTTPError as error:
        detail = error.read(500).decode("utf-8", "replace")
        hint = ""
        try:
            parsed = json.loads(detail)
            hint = str(parsed.get("hint") or parsed.get("detail") or "")[:240]
        except json.JSONDecodeError:
            hint = detail[:180]
        raise HistoryError(f"http_{error.code}", f"El proveedor respondió HTTP {error.code}. {hint}".strip()) from error
    except (URLError, TimeoutError, OSError) as error:
        raise HistoryError("api_error", "No se pudo contactar al proveedor o la consulta caducó.") from error


def _get_json(url, timeout, opener, headers=None, return_headers=False):
    payload, response_headers = _request(url, timeout, opener, headers=headers)
    if return_headers:
        return payload, response_headers
    return payload


def _post_json(url, body, timeout, opener):
    payload, _headers = _request(url, timeout, opener, data=json.dumps(body).encode("utf-8"))
    return payload


def flights_csv(result: dict) -> str:
    rows = ["icao24,callsign,icao_type,engine_class,acoustic_eligible,nearest_ground_km,vertices_outside_radius"]
    for flight in result.get("flights") or []:
        nearest = flight.get("nearest_vertex_in_window") or {}
        ground = nearest.get("ground_m")
        rows.append(
            ",".join(
                [
                    _csv(flight.get("icao24")),
                    _csv(flight.get("callsign")),
                    _csv(flight.get("icao_type")),
                    _csv(flight.get("engine_class")),
                    "si" if flight.get("acoustic_eligible") else "no",
                    "" if ground is None else f"{ground / 1000:.3f}",
                    "si" if flight.get("vertices_outside_requested_radius") else "no",
                ]
            )
        )
    return "\n".join(rows) + "\n"


def _csv(value) -> str:
    text = "" if value is None else str(value)
    if any(character in text for character in ",\"\n"):
        return '"' + text.replace('"', '""') + '"'
    return text
