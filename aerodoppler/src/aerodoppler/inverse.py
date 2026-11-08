"""Bounded inverse search for a recording position.

The prior center is only the middle of the region to be searched. It is never
returned as a recovered microphone. One flyover stays ambiguous: the Doppler
curve does not choose a side of the track. Scores are residuals, not
probabilities, and a hypothesis is not a calibrated location.
"""

from __future__ import annotations

import math
import time

import numpy as np

from .physics import DEFAULT_C, ecef

_FORBIDDEN_PRIOR_KEYS = {
    "truth_lat", "truth_lon", "true_lat", "true_lon",
    "microphone_lat", "microphone_lon", "known_lat", "known_lon",
}


class LocateBudget:
    def __init__(self, max_cells=100, max_flights=8, max_clock_steps=15, max_ridge_points=160, max_seconds=12.0, spacing_m=2500.0):
        self.max_cells = max_cells
        self.max_flights = max_flights
        self.max_clock_steps = max_clock_steps
        self.max_ridge_points = max_ridge_points
        self.max_seconds = max_seconds
        self.spacing_m = spacing_m


def locate_microphone(ridge, flights, prior, recording_start_unix, uncertainty_s, altitude_m, c=DEFAULT_C, budget=None, history_status=None):
    budget = budget or LocateBudget()
    prior = dict(prior or {})
    leaked = _FORBIDDEN_PRIOR_KEYS.intersection(prior)
    if leaked:
        raise ValueError("La búsqueda no acepta la posición verdadera del micrófono.")
    base = {
        "unique_identification": False,
        "calibrated_confidence": False,
        "prior_is_result": False,
        "reported_point": None,
        "hypotheses": [],
        "propagation": "El tiempo de recepción del modelo es emisión más distancia/c.",
        "altitude_assumption_m": None if altitude_m is None else float(altitude_m),
        "altitude_note": "Una sola altitud supuesta para toda la zona. No es una cota medida del micrófono ni un modelo de terreno por celda.",
    }
    if history_status in {"api_error", "unsupported_history", "unsupported_query", "unknown_coverage", "auth_required", "plan_limit", "unparsed_provider_response"}:
        return {**base, "status": "incomplete", "reason": history_status,
                "message": "No hay una búsqueda de posición: la consulta histórica no está completa."}
    usable, reason = _usable_ridge(ridge)
    if not usable:
        return {**base, "status": "no_usable_event", "reason": reason,
                "message": "El audio no aporta una pista Doppler utilizable. No se estima una posición."}
    if altitude_m is None or not np.isfinite(float(altitude_m)):
        return {**base, "status": "needs_altitude", "reason": "altitude_missing",
                "message": "Hace falta una altitud supuesta, en metros, para toda la zona de búsqueda."}
    try:
        lat = float(prior["lat"])
        lon = float(prior["lon"])
        radius_m = float(prior["radius_km"]) * 1000.0
        uncertainty_s = float(uncertainty_s)
        start = float(recording_start_unix)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("La zona de búsqueda o el reloj no son válidos.") from error
    if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 1000 <= radius_m <= 40000):
        raise ValueError("La zona de búsqueda admite de 1 a 40 km de radio.")
    if not 0 <= uncertainty_s <= 3600:
        raise ValueError("La incertidumbre del reloj admite de 0 a 60 min.")
    eligible, excluded = _split_flights(flights or [])
    base["flights_received"] = len(flights or [])
    base["eligible_flights"] = len(eligible)
    base["excluded_flights"] = excluded
    base["zero_flights_claim"] = False
    if not flights:
        return {**base, "status": "no_eligible_aircraft", "reason": "no_flights_in_query",
                "message": "La consulta no devolvió trayectorias. Eso no demuestra que no hubiera aviones, y no hay posición."}
    if not eligible:
        return {**base, "status": "no_eligible_aircraft", "reason": "jets_or_unknown_excluded",
                "message": "Hay trayectorias, pero ninguna es de un avión no reactor con tipo confirmado. No se estima una posición."}
    if len(eligible) > budget.max_flights:
        return {**base, "status": "incomplete", "reason": "flight_budget",
                "eligible_not_scored": len(eligible) - budget.max_flights,
                "message": "Hay más aviones elegibles que el cupo de esta búsqueda. El resultado queda incompleto, sin punto."}
    cells, spacing, collapsed = _cells(lat, lon, radius_m, budget.spacing_m, budget.max_cells)
    base["resolution_m"] = spacing
    base["cells_planned"] = len(cells)
    base["prior"] = {"lat": lat, "lon": lon, "radius_km": radius_m / 1000.0, "role": "zona de búsqueda, no micrófono"}
    if collapsed:
        return {**base, "status": "incomplete", "reason": "grid_too_coarse",
                "message": "La zona no cabe en el cupo de celdas sin volverse más gruesa que la propia búsqueda. No se devuelve el centro."}
    prepared = [(flight, _prepare(flight)) for flight in eligible]
    prepared = [(flight, segments) for flight, segments in prepared if segments]
    if not prepared:
        return {**base, "status": "no_eligible_aircraft", "reason": "tracks_too_short",
                "message": "Las trayectorias elegibles no tienen tramos continuos bastante largos. No se estima una posición."}
    ridge_t, ridge_f = _downsample_ridge(ridge, budget.max_ridge_points)
    steps = min(budget.max_clock_steps, max(1, int(math.ceil(2 * uncertainty_s / 2.0)) + 1))
    if uncertainty_s == 0:
        shifts = np.array([0.0])
    else:
        shifts = np.linspace(-uncertainty_s, uncertainty_s, steps)
    base["clock_step_s"] = float(shifts[1] - shifts[0]) if len(shifts) > 1 else 0.0
    started = time.perf_counter()
    scored = []
    for cell_lat, cell_lon in cells:
        if time.perf_counter() - started > budget.max_seconds:
            return {**base, "status": "incomplete", "reason": "time_budget", "cells_scored": len(scored),
                    "message": "Se agotó el tiempo de búsqueda. No se devuelve un punto parcial."}
        observer = ecef(cell_lat, cell_lon, float(altitude_m)).reshape(3)
        flight_fits = []
        for flight, segments in prepared:
            fit = _best_shift(observer, segments, ridge_t, ridge_f, start, shifts, c)
            if fit is not None:
                flight_fits.append((flight, fit))
        if not flight_fits:
            continue
        flight_fits.sort(key=lambda item: item[1]["rmse_hz"])
        best_fit = flight_fits[0][1]
        supporting = [item for item in flight_fits if item[1]["rmse_hz"] <= 12.0 and item[1]["r2"] >= 0.75]
        scored.append({
            "lat": cell_lat,
            "lon": cell_lon,
            "rmse_hz": best_fit["rmse_hz"],
            "r2": best_fit["r2"],
            "clock_shift_s": best_fit["clock_shift_s"],
            "f0_hz": best_fit["f0_hz"],
            "supporting_flights": len({item[0].get("icao24") or item[0].get("flight_id") for item in supporting}),
            "supported": bool(supporting),
            "best_callsign": flight_fits[0][0].get("callsign"),
        })
    base["cells_scored"] = len(cells)
    base["elapsed_s"] = round(time.perf_counter() - started, 3)
    if not scored:
        return {**base, "status": "no_supported_hypothesis", "reason": "no_time_overlap",
                "message": "Ninguna celda produjo solape entre la pista y una trayectoria elegible."}
    scored = [item for item in scored if item["supported"]] or scored
    scored.sort(key=lambda item: (not item["supported"], item["rmse_hz"]))
    best = scored[0]
    if not best["supported"] or best["rmse_hz"] > 12.0 or best["r2"] < 0.75:
        return {**base, "status": "no_supported_hypothesis", "reason": "residual_too_large",
                "best_rmse_hz": best["rmse_hz"], "best_r2": best["r2"],
                "message": "La celda menos mala no se parece a un paso con retardo. No se publica como posición."}
    band = max(2.0, 0.15 * best["rmse_hz"])
    near = [item for item in scored if item["rmse_hz"] <= best["rmse_hz"] + band][:8]
    spread = _spread_m(near)
    single_pass = best["supporting_flights"] < 2
    separated = spread > 2.5 * spacing
    for item in near:
        item["resolution_m"] = spacing
        item["role"] = "hipótesis de celda"
    base["hypotheses"] = near
    base["resolution_m"] = spacing
    if single_pass or separated or len(near) > 1:
        return {**base, "status": "ambiguous", "reason": "single_pass_or_near_tie" if single_pass or len(near) > 1 else "separated_cells",
                "message": "Hay hipótesis compatibles, no una coordenada. Una sola pasada no elige el lado de la trayectoria, y un empate tampoco."}
    return {**base, "status": "hypothesis", "reason": "multiple_flights_one_cell",
            "message": "Varias trayectorias elegibles prefieren una celda. Sigue sin ser una posición calibrada ni única."}


def _usable_ridge(ridge):
    if not ridge or not isinstance(ridge, dict):
        return False, "missing_ridge"
    if ridge.get("status") == "rejected":
        return False, "ridge_rejected"
    times = ridge.get("time_s")
    freqs = ridge.get("frequency_hz")
    if not times or not freqs or len(times) != len(freqs) or len(times) < 8:
        return False, "ridge_too_short"
    if float(times[-1]) - float(times[0]) < 4.0:
        return False, "ridge_too_short"
    return True, None


def _split_flights(flights):
    eligible, excluded = [], {"jet": 0, "unknown": 0, "other": 0}
    for flight in flights:
        if flight.get("acoustic_eligible") is True and flight.get("engine_class") not in {"jet", "unknown"}:
            eligible.append(flight)
        else:
            kind = flight.get("engine_class") or "other"
            excluded[kind] = excluded.get(kind, 0) + 1
    return eligible, excluded


def _cells(lat, lon, radius_m, spacing_m, max_cells):
    spacing = float(spacing_m)
    while True:
        cells = _ring(lat, lon, radius_m, spacing)
        if len(cells) <= max_cells:
            collapsed = spacing > radius_m * 0.67
            return cells, spacing, collapsed
        spacing *= 1.4
        if spacing > radius_m:
            return [], spacing, True


def _ring(lat, lon, radius_m, spacing):
    ky = 110570.0
    kx = 111320.0 * math.cos(math.radians(lat))
    steps = int(math.floor(radius_m / spacing))
    cells = []
    for iy in range(-steps, steps + 1):
        for ix in range(-steps, steps + 1):
            north = iy * spacing
            east = ix * spacing
            if east * east + north * north > radius_m * radius_m:
                continue
            cells.append((lat + north / ky, lon + east / max(kx, 1.0)))
    return cells


def _prepare(flight):
    points = []
    for point in flight.get("points") or []:
        try:
            stamp, lat, lon, alt = float(point["t"]), float(point["lat"]), float(point["lon"]), float(point["alt_m"])
        except (KeyError, TypeError, ValueError):
            continue
        if np.isfinite([stamp, lat, lon, alt]).all():
            points.append((stamp, lat, lon, alt))
    points.sort()
    groups, current, previous = [], [], None
    for point in points:
        if previous is not None and point[0] - previous > 70.0 and current:
            groups.append(current)
            current = []
        current.append(point)
        previous = point[0]
    if current:
        groups.append(current)
    segments = []
    for group in groups:
        if len(group) < 3:
            continue
        times = np.array([item[0] for item in group])
        if np.any(np.diff(times) <= 0):
            continue
        xyz = ecef([item[1] for item in group], [item[2] for item in group], [item[3] for item in group])
        velocity = np.gradient(xyz, times, axis=0)
        segments.append((times, xyz, velocity))
    return segments


def _downsample_ridge(ridge, limit):
    times = np.asarray(ridge["time_s"], dtype=float)
    freqs = np.asarray(ridge["frequency_hz"], dtype=float)
    if len(times) > limit:
        index = np.linspace(0, len(times) - 1, limit).astype(int)
        times, freqs = times[index], freqs[index]
    return times, freqs


def _best_shift(observer, segments, ridge_t, ridge_f, start, shifts, c):
    best = None
    for shift in shifts:
        observed_t = start + shift + ridge_t
        observed_parts, shape_parts, span_parts = [], [], []
        for times, xyz, velocity in segments:
            relative = xyz - observer
            distance = np.linalg.norm(relative, axis=1)
            speed = np.linalg.norm(velocity, axis=1)
            if np.any(distance < 1.0) or np.any(speed >= c) or np.any(~np.isfinite(distance)):
                continue
            radial = np.sum(velocity * relative, axis=1) / distance
            receive = times + distance / c
            if np.any(np.diff(receive) <= 0):
                continue
            mask = (observed_t >= receive[0]) & (observed_t <= receive[-1])
            if int(np.count_nonzero(mask)) < 4:
                continue
            shape = c / (c + radial)
            observed_parts.append(ridge_f[mask])
            shape_parts.append(np.interp(observed_t[mask], receive, shape))
            span_parts.append(observed_t[mask])
        if not observed_parts:
            continue
        observed = np.concatenate(observed_parts)
        shape = np.concatenate(shape_parts)
        span = np.concatenate(span_parts)
        needed = max(6.0, min(20.0, 0.45 * float(ridge_t[-1] - ridge_t[0])))
        if float(span.max() - span.min()) < needed:
            continue
        denom = float(np.dot(shape, shape))
        if denom <= 0:
            continue
        f0 = float(np.dot(observed, shape) / denom)
        if not math.isfinite(f0) or f0 <= 0:
            continue
        residual = observed - f0 * shape
        rmse = float(np.sqrt(np.mean(residual ** 2)))
        ss = float(np.sum((observed - np.mean(observed)) ** 2))
        r2 = float(1.0 - np.sum(residual ** 2) / ss) if ss > 0 else 0.0
        trial = {"rmse_hz": rmse, "r2": r2, "clock_shift_s": float(shift), "f0_hz": f0}
        if best is None or trial["rmse_hz"] < best["rmse_hz"]:
            best = trial
    return best


def _spread_m(cells):
    if len(cells) < 2:
        return 0.0
    best = 0.0
    for left in cells:
        for right in cells:
            best = max(best, _haversine(left["lat"], left["lon"], right["lat"], right["lon"]))
    return best


def _haversine(lat1, lon1, lat2, lon2):
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def hypotheses_csv(result):
    rows = ["lat,lon,rmse_hz,r2,clock_shift_s,resolution_m,supporting_flights"]
    for item in result.get("hypotheses") or []:
        rows.append(",".join(str(item.get(key, "")) for key in ("lat", "lon", "rmse_hz", "r2", "clock_shift_s", "resolution_m", "supporting_flights")))
    return "\n".join(rows) + "\n"
