"""Wide-area plan for finding where a clip was recorded.

adsb.aero is queried as a circle. This service allows at most 120 km and a
few pages, so peninsular Spain and the Balearics are many circles, not one.
The free web process does not run that sweep and does not download the
multi-gigabyte adsb.lol daily archive. A local command can query one tile at
a time; only the area and the time window are sent to the provider.

The inverse search itself is unchanged: coarse cells, then a finer patch, and
the same ok / weak / ambiguous / failed verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

from .assessment import failed_assessment
from .inverse import LocateBudget, locate_microphone
from .provinces import attribution, communities, cover_selection, neighbors_of, province_record
from .regions import ASSUMED_ALTITUDE_M, region_tiles, resolve_region

# One complete adsb.aero tile is the archive-range call plus up to 3 result
# pages (4 calls) and one inverse pass. At the 25 s history cap that is
# 4*25 + 12 = 112 s, inside a 120 s in-process job on the free instance.
# A second tile doubles the worst case past that budget. Starting it and
# stopping halfway would look like a search of the whole selection, so the
# web runs a selection only when a single circle covers it.
WEB_MAX_TILES = 1
WEB_MAX_CALLS = 4
WEB_MAX_SECONDS = 120
INVERSE_SECONDS = 12
LOCAL_COMMAND_PLAN = "python -m aerodoppler.wide_search --plan"
LOCAL_COMMAND_RUN = "python -m aerodoppler.wide_search --audio ARCHIVO --start 2026-11-07T23:00 --uncertainty-h 2 --provincia ID"


def plan_cost(region_id: str | None, settings) -> dict:
    text = (region_id or "").strip().lower()
    if text in {"", "unsure", "no-se", "nose", "no se", "no sé", "iberia", "espana", "españa"}:
        region = region_tiles(resolve_region(text or None))
    else:
        region = cover_selection(region_id)
    return annotate_plan(region, settings)


def annotate_plan(region: dict, settings) -> dict:
    pages = int(settings.max_history_pages)
    timeout_s = float(settings.history_timeout_s)
    # Each tile is one history call: the archive range plus up to `pages` result pages.
    queries = region["tile_count"] * (1 + pages)
    network_s = queries * timeout_s
    search_s = network_s + region["tile_count"] * INVERSE_SECONDS
    fits = region["tile_count"] <= WEB_MAX_TILES and queries <= WEB_MAX_CALLS and search_s <= WEB_MAX_SECONDS
    return {
        **region,
        "provider": "adsb.aero",
        "queries": queries,
        "queries_note": (
            f"Cada una de las {region['tile_count']} teselas repite el rango de archivo y, en el peor caso, "
            f"{pages} páginas de resultados. No se descarga el archivo diario global."
        ),
        "time_bound_s": network_s,
        "search_bound_s": search_s,
        "time_note": (
            f"Cota si cada llamada agota los {timeout_s:.0f} s y cada tesela usa {INVERSE_SECONDS} s de puntuación. "
            "No es un tiempo medido contra el proveedor."
        ),
        "fits_free_web": fits,
        "web_max_tiles": WEB_MAX_TILES,
        "web_max_calls": WEB_MAX_CALLS,
        "web_max_seconds": WEB_MAX_SECONDS,
    }


def region_menu(settings) -> dict:
    groups = []
    for community in communities():
        rows = []
        if len(community["provinces"]) > 1:
            plan = plan_cost("cc:" + community["id"], settings)
            rows.append(_menu_item(plan, "cc:" + community["id"], "Toda " + community["name"], community["id"]))
        for province_id in community["provinces"]:
            record = province_record(province_id)
            plan = plan_cost(province_id, settings)
            rows.append({
                **_menu_item(plan, province_id, record["name"], community["id"]),
                "neighbors": neighbors_of(province_id),
            })
        groups.append({"id": community["id"], "name": community["name"], "items": rows})
    iberia = plan_cost("unsure", settings)
    return {
        "attribution": attribution(),
        "groups": groups,
        "default": _menu_item(iberia, "unsure", "No sé. Por defecto, España peninsular y Baleares", None),
    }


def _menu_item(plan, item_id, name, community) -> dict:
    return {
        "id": item_id,
        "name": name,
        "community": community,
        "label": plan["label"],
        "tile_count": plan["tile_count"],
        "queries": plan["queries"],
        "time_bound_s": plan["time_bound_s"],
        "search_bound_s": plan["search_bound_s"],
        "fits_free_web": plan["fits_free_web"],
        "tiles": [{"lat": tile["lat"], "lon": tile["lon"], "radius_km": tile["radius_km"], "name": tile["name"]} for tile in plan["tiles"]],
        "map": plan["map"],
        "why": plan.get("why"),
    }


def free_tier_response(region_id: str | None, settings, uncertainty_h: float | None = None) -> dict:
    """Honest refusal for the free web process. Does not call the network."""
    plan = plan_cost(region_id, settings)
    hours = None if uncertainty_h is None else float(uncertainty_h)
    window_min = None if hours is None else abs(hours) * 2 * 60.0
    window_note = None
    if window_min is not None and window_min > settings.max_window_min:
        window_note = (
            f"La ventana de ±{hours:g} h no cabe en una consulta: el máximo de este servicio es "
            f"{settings.max_window_min:.0f} min."
        )
    clock_note = (
        "Con una incertidumbre de horas, aunque la búsqueda llegara a ejecutarse, el reloj se muestrearía "
        "en pasos de muchos minutos. Eso no fija el segundo del sobrevuelo."
    )
    message = (
        f"Averiguar dónde se grabó en {plan['label']} no cabe en el plan gratuito. "
        f"Harían falta {plan['queries']} llamadas a adsb.aero "
        f"({plan['tile_count']} teselas de {plan['tile_radius_km']:.0f} km; cada una pide el rango del archivo y hasta {settings.max_history_pages} páginas) "
        f"y una cota de {plan['time_bound_s']:.0f} s solo de red. "
        "Este servicio no lanza esa búsqueda y no descarga el archivo diario de varios GB. "
        "En tu ordenador, sin subir el audio a terceros, puedes usar el modo local."
    )
    if window_note:
        message = window_note + " " + message
    empty = failed_assessment(message)
    return {
        **empty,
        "status": "incomplete",
        "reason": "wide_area_not_on_free_tier",
        "summary": message,
        "message": message,
        "prior_is_result": False,
        "reported_point": None,
        "show_point": False,
        "point_is_certain": False,
        "hypotheses": [],
        "unique_identification": False,
        "calibrated_confidence": False,
        "zero_flights_claim": False,
        "region": {"id": plan["id"], "label": plan["label"], "why": plan["why"], "role": plan["role"]},
        "map": plan["map"],
        "plan": {
            "tiles": plan["tiles"],
            "tile_count": plan["tile_count"],
            "tile_radius_km": plan["tile_radius_km"],
            "queries": plan["queries"],
            "queries_note": plan["queries_note"],
            "time_bound_s": plan["time_bound_s"],
            "time_note": plan["time_note"],
            "fits_free_web": False,
            "assumed_altitude_m": plan["assumed_altitude_m"],
            "altitude_note": plan["altitude_note"],
            "window_note": window_note,
            "clock_note": clock_note,
            "search_bound_s": plan.get("search_bound_s"),
        },
        "local_mode": {
            "available": True,
            "runs_on": "ordenador local, no en Render y sin cargos del servicio",
            "plan_command": _plan_command(plan),
            "run_command": _run_command(plan),
            "note": (
                "El modo local pide a adsb.aero un círculo y una ventana por tesela, de una en una, y se detiene a los 15 min diciendo qué teselas no consultó. "
                "No descarga el archivo diario. El audio se queda en el ordenador."
            ),
        },
    }


def _plan_command(plan) -> str:
    ident = plan.get("id")
    if ident in (None, "iberia"):
        return LOCAL_COMMAND_PLAN
    return LOCAL_COMMAND_PLAN + " --provincia " + str(ident)


def _run_command(plan) -> str:
    base = LOCAL_COMMAND_RUN.split(" --provincia")[0]
    ident = plan.get("id")
    if ident in (None, "iberia"):
        return base
    return base + " --provincia " + str(ident)


def execute_covered_plan(plan, start: datetime, end: datetime, settings, ridge, recording_unix, uncertainty_s, altitude_m, opener, events=None, progress=None):
    """Run every tile of a plan that already fits the web budget.

    A truncated provider list is not scored: a partial flight list is not
    coverage of the circle.
    """
    if not plan.get("fits_free_web") or len(plan.get("tiles") or []) != 1:
        raise ValueError("Esta selección no se ejecuta en la web.")
    tile = plan["tiles"][0]
    if progress:
        progress("Consultando adsb.aero en la tesela.", 0)
    from .history import query_history

    history = query_history("adsb_aero", float(tile["lat"]), float(tile["lon"]), float(tile["radius_km"]), start, end, settings, opener)
    if history.get("truncated") or history.get("status") == "partial":
        return _stopped(plan, "adsb.aero cortó la lista de vuelos. No se publica una posición con una consulta incompleta.")
    if history.get("status") not in {"ok", "no_matches"}:
        return _stopped(plan, history.get("message") or "La consulta histórica no está completa.")
    if progress:
        progress("Puntuando la pista dentro de la zona elegida.", 0)
    kept = filter_flights(history.get("flights") or [], tile, start.timestamp(), end.timestamp())
    found = score_tile(tile, kept, ridge, recording_unix, uncertainty_s, altitude_m, events=events)
    found["executed"] = True
    found["coverage_note"] = plan.get("why")
    found["region"] = {"id": plan.get("id"), "label": plan.get("label"), "why": plan.get("why"), "role": plan.get("role")}
    found["map"] = plan.get("map")
    found["plan"] = {
        "tiles": plan["tiles"],
        "tile_count": plan["tile_count"],
        "queries": plan["queries"],
        "time_bound_s": plan["time_bound_s"],
        "search_bound_s": plan.get("search_bound_s"),
        "fits_free_web": True,
        "executed": True,
        "tile_radius_km": plan.get("tile_radius_km"),
    }
    found["point_is_certain"] = False
    return found


def _stopped(plan, message: str) -> dict:
    empty = failed_assessment(message)
    return {
        **empty,
        "status": "incomplete",
        "reason": "provider_incomplete",
        "summary": message,
        "message": message,
        "show_point": False,
        "reported_point": None,
        "point_is_certain": False,
        "hypotheses": [],
        "executed": False,
        "region": {"id": plan.get("id"), "label": plan.get("label"), "why": plan.get("why"), "role": plan.get("role")},
        "map": plan.get("map"),
        "plan": {"tile_count": plan.get("tile_count"), "fits_free_web": True, "executed": False},
        "coverage_note": plan.get("why"),
    }


def filter_flights(flights, tile, start_unix: float, end_unix: float, margin_s: float = 600.0) -> list[dict]:
    """Keep tracks that pass near the tile during the recording window.

    Jets and unknown types stay out of the acoustic search. A track with no
    sample in the window cannot explain the clip.
    """
    kept = []
    radius_m = (float(tile["radius_km"]) + 30.0) * 1000.0
    for flight in flights or []:
        if flight.get("acoustic_eligible") is not True or flight.get("engine_class") in {"jet", "unknown"}:
            continue
        points = flight.get("points") or []
        times = []
        near = False
        for point in points:
            try:
                stamp = float(point["t"])
                lat = float(point["lat"])
                lon = float(point["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            times.append(stamp)
            if _km(tile["lat"], tile["lon"], lat, lon) * 1000.0 <= radius_m:
                near = True
        if not times or not near:
            continue
        if max(times) < start_unix - margin_s or min(times) > end_unix + margin_s:
            continue
        kept.append(flight)
    return kept


def score_tile(tile, flights, ridge, start_unix, uncertainty_s, altitude_m, events=None, budget=None):
    """Score one circle with the existing inverse engine."""
    budget = budget or LocateBudget(max_cells=80, max_seconds=8, spacing_m=4000, max_flights=8)
    altitude = ASSUMED_ALTITUDE_M if altitude_m is None else altitude_m
    found = locate_microphone(
        ridge,
        flights,
        {"lat": tile["lat"], "lon": tile["lon"], "radius_km": tile["radius_km"]},
        start_unix,
        uncertainty_s,
        altitude,
        budget=budget,
        events=events,
    )
    found["tile"] = {"name": tile.get("name"), "lat": tile["lat"], "lon": tile["lon"], "radius_km": tile["radius_km"]}
    found["point_is_certain"] = False
    return found


def run_local_tiles(tiles, start: datetime, end: datetime, settings, ridge, recording_unix, uncertainty_s, altitude_m, opener, max_seconds: float = 90.0, events=None):
    """Query one tile at a time. Stop when the time budget is spent and say so.

    ``opener`` is injectable. The web service does not call this.
    """
    from .history import query_history

    started = time.perf_counter()
    scored = []
    skipped = []
    queries = 0
    for tile in tiles:
        if time.perf_counter() - started > max_seconds:
            skipped.append(tile["name"])
            continue
        history = query_history("adsb_aero", tile["lat"], tile["lon"], tile["radius_km"], start, end, settings, opener)
        queries += 1
        if history.get("status") not in {"ok", "no_matches", "partial"}:
            skipped.append(tile["name"])
            continue
        kept = filter_flights(history.get("flights") or [], tile, start.timestamp(), end.timestamp())
        if not kept:
            continue
        found = score_tile(tile, kept, ridge, recording_unix, uncertainty_s, altitude_m, events=events)
        scored.append(found)
        if time.perf_counter() - started > max_seconds:
            break
    return {
        "tiles_requested": len(tiles),
        "tiles_scored": len(scored),
        "tiles_skipped": skipped,
        "provider_queries": queries,
        "incomplete": bool(skipped),
        "results": scored,
        "archive_download": False,
    }


def on_render() -> bool:
    return bool(os.environ.get("RENDER") or os.environ.get("RENDER_SERVICE_ID"))


def _km(lat1, lon1, lat2, lon2) -> float:
    from .regions import _km as region_km

    return region_km(lat1, lon1, lat2, lon2)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Plan or run a local wide-area search. Not used by the web service.")
    parser.add_argument("--plan", action="store_true", help="Print the tile plan and exit. No network.")
    parser.add_argument("--region", default="unsure")
    parser.add_argument("--provincia", default=None, help="Id de provincia, varias contiguas separadas por coma, o cc:AN")
    parser.add_argument("--audio", default=None)
    parser.add_argument("--start", default=None, help="Approximate start, Europe/Madrid, YYYY-MM-DDTHH:MM")
    parser.add_argument("--uncertainty-h", type=float, default=2.0)
    args = parser.parse_args(argv)
    from .config import load_settings

    settings = load_settings()
    plan = plan_cost(args.provincia or args.region, settings)
    if args.plan or not args.audio:
        brief = {
            "label": plan["label"],
            "why": plan["why"],
            "tile_count": plan["tile_count"],
            "tile_radius_km": plan["tile_radius_km"],
            "queries": plan["queries"],
            "time_bound_s": plan["time_bound_s"],
            "time_note": plan["time_note"],
            "fits_free_web": plan["fits_free_web"],
            "archive_download": False,
        }
        json.dump(brief, sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
        return 0
    if on_render():
        sys.stderr.write("En Render no se lanza el barrido de teselas.\n")
        return 2
    if not args.start:
        sys.stderr.write("Hace falta --start con la hora aproximada, Europe/Madrid.\n")
        return 2
    from datetime import timezone
    from .app import _parse_local
    from .audio_engine import analyze_samples, decode_audio
    from pathlib import Path

    center = _parse_local(args.start)
    half = timedelta(hours=float(args.uncertainty_h))
    start, end = center - half, center + half
    if (end - start).total_seconds() / 60.0 > settings.max_window_min:
        sys.stderr.write("La ventana no cabe en el máximo de una consulta.\n")
        return 2
    path = Path(args.audio)
    samples = decode_audio("ffmpeg", "ffprobe", path, settings.sample_rate, settings.max_duration_s, settings.ffmpeg_timeout_s)
    audio = analyze_samples(samples, settings.sample_rate)
    if audio.get("status") != "accepted":
        json.dump({"status": "no_usable_event", "message": audio.get("message"), "verdict": "failed"}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    from urllib.request import urlopen

    report = run_local_tiles(
        plan["tiles"], start.astimezone(timezone.utc), end.astimezone(timezone.utc), settings,
        {**audio["ridge"], "status": "accepted"}, center.timestamp(), half.total_seconds(), None, urlopen, max_seconds=900.0,
    )
    report["region"] = plan["label"]
    json.dump({key: report[key] for key in ("tiles_requested", "tiles_scored", "tiles_skipped", "provider_queries", "incomplete", "archive_download", "region")}, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
