"""Honest verdict and 68/90 percent regions for a microphone search.

Adapted from the assessment in Espaye/aircraft-doppler (MIT, Copyright 2026
Espaye): a verdict is the worst of ok / weak / ambiguous / failed, a second
local minimum within a small cost margin is ambiguous, two explained passes
are only weak, and the quoted radius is a spread over events rather than the
precision of the optimiser. The numeric thresholds here are for this grid and
this residual in hertz. They are not a copy of that program's Cauchy costs,
and they are not a calibration on real recordings.

The shared clock across events follows the idea in
AlanBacker/audio-flyover-geolocation (MIT, Copyright 2026 AlanBacker): the
recording fixes the time between flyovers, so one offset is estimated for
every event together. This module does not run either repository.
"""

from __future__ import annotations

import math

import numpy as np

VERDICT_LABEL = {
    "ok": "Compatible",
    "weak": "Débil",
    "ambiguous": "Ambiguo",
    "failed": "Insuficiente",
}
_ORDER = ("ok", "weak", "ambiguous", "failed")
# Residual scale for the pseudo-posterior. N_EFF is deliberately small: the
# samples of one Doppler curve are not independent. Neither constant is fitted
# to a real recording.
_N_EFF = 2.0
_SCALE_HZ = 2.0
_ALT_MARGIN_HZ = 2.0
_BOOTSTRAP_DRAWS = 80
_MODE_STABILITY = 0.8
METHOD_NOTE = (
    "Veredicto, mínimos alternativos y radios del 68 % y del 90 % adaptados de "
    "Espaye/aircraft-doppler (MIT, Copyright 2026 Espaye). El desfase común a dos o más "
    "sobrevuelos adapta la idea de AlanBacker/audio-flyover-geolocation (MIT, Copyright 2026 AlanBacker). "
    "No se ejecuta el código de esos repositorios. Terreno plano: una altitud supuesta, sin USGS."
)


def failed_assessment(message: str | None) -> dict:
    text = message or "Los datos no sostienen una posición."
    return {
        "verdict": "failed",
        "verdict_label": VERDICT_LABEL["failed"],
        "summary": text,
        "flags": [{"level": "failed", "message": text}],
        "regions": _empty_regions(),
        "clock": _clock(False, None, None, None),
        "show_point": False,
        "reported_point": None,
        "point_is_certain": False,
        "events_explained": 0,
    }


def assess_cells(cells, *, n_events, spacing_m, prior_lat, prior_lon, radius_m, shifts, cost_cube=None):
    """Score an already computed grid. ``cost_cube`` is (cells, events, shifts) or None."""
    if not cells:
        return failed_assessment("Ninguna celda produjo un ajuste.")
    costs = np.array([float(item["cost"]) for item in cells], dtype=float)
    explained = np.array([int(item["explained"]) for item in cells], dtype=int)
    order = np.argsort(np.where(np.isfinite(costs), costs, 1e9))
    best_i = int(order[0])
    best = cells[best_i]
    best_cost = float(costs[best_i])
    best_explained = int(explained[best_i])
    flags = []

    def flag(level, message):
        flags.append({"level": level, "message": message})

    if best_explained < 1 or not math.isfinite(best_cost):
        flag("failed", "Ningún evento queda explicado por una trayectoria elegible en la mejor celda.")
    elif best_explained < 2 or n_events < 2:
        flag(
            "ambiguous",
            "Un solo sobrevuelo no elige el lado de la trayectoria ni separa el lugar del desfase del reloj.",
        )
    elif best_explained == 2:
        flag("weak", "Solo dos eventos sostienen la hipótesis. Es un indicio, no una coordenada segura.")

    floor_m = float(spacing_m) / math.sqrt(2.0)
    posterior = _posterior(costs)
    post_68 = _credible_radius(cells, posterior, best_i, 0.68, floor_m)
    post_90 = _credible_radius(cells, posterior, best_i, 0.90, max(floor_m, post_68))
    boot = _bootstrap(cost_cube, shifts, cells, best_i, floor_m, float(spacing_m))
    radius_68 = post_68
    radius_90 = post_90
    if boot is not None:
        radius_68 = max(radius_68, boot["radius_68_m"])
        radius_90 = max(radius_90, boot["radius_90_m"], radius_68)
        if boot["stability"] < _MODE_STABILITY:
            level = "weak" if n_events < 5 else "ambiguous"
            flag(
                level,
                f"La zona mejor cambia en el {100.0 * (1.0 - boot['stability']):.0f} % de los remuestreos de eventos.",
            )
    if post_90 > 0.5 * float(radius_m):
        flag("ambiguous", "La región del 90 % cubre gran parte de la zona de búsqueda. No hay un sitio acotado.")

    modes = _other_modes(cells, costs, best_i, float(spacing_m), best_cost)
    if modes:
        distance = _haversine(best["lat"], best["lon"], modes[0]["lat"], modes[0]["lon"])
        flag(
            "ambiguous",
            f"Hay otro mínimo a {distance / 1000.0:.1f} km que encaja casi igual ({modes[0]['explained']} eventos). No hay una posición fiable.",
        )
    if cost_cube is not None and cost_cube.shape[1] >= 2 and best_explained >= 2:
        for moved in _leave_one_out(cost_cube, cells, best_i, float(spacing_m)):
            flag("weak", f"Al quitar el evento {moved + 1} la solución se mueve a otra zona.")
            break
    if _haversine(best["lat"], best["lon"], prior_lat, prior_lon) > 0.9 * float(radius_m):
        flag("weak", "La hipótesis cae en el borde del disco de búsqueda. Conviene ampliar el radio.")

    verdict = max((item["level"] for item in flags), key=_ORDER.index, default="ok")
    if verdict == "ok" and best_explained < 3:
        verdict = "weak" if best_explained == 2 else "ambiguous"
    if verdict == "ok" and max(post_90, radius_90) > 0.35 * float(radius_m):
        verdict = "weak"
        flag("weak", "El radio del 90 % es grande frente a la zona de búsqueda. La hipótesis queda como indicio.")
    summary = {
        "ok": "Los eventos coinciden en una zona. El radio describe esa rejilla, no una calibración con grabaciones reales.",
        "weak": "Hay una hipótesis, pero la evidencia es débil. No es una coordenada segura.",
        "ambiguous": "Varias zonas explican los datos de forma parecida, o falta un segundo sobrevuelo. No hay un punto fiable.",
        "failed": "Los datos no sostienen una posición.",
    }[verdict]
    show = verdict in {"ok", "weak"}
    point = None
    if show:
        point = {
            "lat": float(best["lat"]),
            "lon": float(best["lon"]),
            "role": "hipótesis de celda, no posición calibrada",
        }
    shift = float(best["shift"]) if best.get("shift") is not None else None
    shared = bool(n_events >= 2 and best_explained >= 2 and verdict != "failed" and shift is not None)
    spread = _shift_spread(cost_cube, best_i, shifts) if shared else None
    return {
        "verdict": verdict,
        "verdict_label": VERDICT_LABEL[verdict],
        "summary": summary,
        "flags": flags,
        "regions": {
            "68": {"level": 0.68, "radius_m": float(radius_68), "cells": _cells_within(cells, posterior, best_i, radius_68)},
            "90": {"level": 0.90, "radius_m": float(radius_90), "cells": _cells_within(cells, posterior, best_i, radius_90)},
            "calibrated": False,
            "grid_floor_m": float(floor_m),
            "method": (
                "El radio es el máximo entre la pseudo-posterior del residuo, "
                "el percentil bootstrap por evento (80 remuestreos, semilla fija) y el semidiagonal de la celda. "
                "No es una probabilidad calibrada."
            ),
        },
        "clock": _clock(shared, shift if shared else None, _step(shifts), spread),
        "show_point": show,
        "reported_point": point,
        "point_is_certain": False,
        "events_explained": best_explained,
    }


def _empty_regions():
    return {
        "68": {"level": 0.68, "radius_m": None, "cells": []},
        "90": {"level": 0.90, "radius_m": None, "cells": []},
        "calibrated": False,
        "grid_floor_m": None,
        "method": "Sin región: la evidencia no llegó a un ajuste.",
    }


def _clock(shared, offset, step, spread):
    if shared:
        note = (
            "Desfase común a todos los eventos. La grabación fija el tiempo entre sobrevuelos; "
            "se busca un solo offset dentro de la ventana indicada."
        )
    else:
        note = "Con menos de dos eventos explicados no se publica un desfase de reloj: se confunde con el lugar."
    return {
        "shared": bool(shared),
        "offset_s": None if offset is None else float(offset),
        "step_s": None if step is None else float(step),
        "spread_68_s": None if spread is None else float(spread),
        "note": note,
    }


def _step(shifts):
    if shifts is None or len(shifts) < 2:
        return 0.0
    return float(shifts[1] - shifts[0])


def _posterior(costs):
    finite = np.isfinite(costs)
    if not finite.any():
        return np.zeros(costs.shape)
    delta = np.where(finite, costs - np.min(costs[finite]), 1e6)
    logw = -0.5 * _N_EFF * (delta / _SCALE_HZ)
    logw = np.where(finite, logw, -1e6)
    logw -= np.max(logw)
    weights = np.exp(logw)
    total = float(weights.sum())
    if total <= 0:
        return np.zeros(costs.shape)
    return weights / total


def _credible_radius(cells, weights, best_i, level, floor_m):
    best = cells[best_i]
    dist = np.array([
        _haversine(best["lat"], best["lon"], item["lat"], item["lon"]) for item in cells
    ])
    order = np.argsort(dist)
    cumulative = np.cumsum(weights[order])
    index = int(np.searchsorted(cumulative, level, side="left"))
    index = min(index, len(order) - 1)
    return max(float(floor_m), float(dist[order[index]]))


def _cells_within(cells, weights, best_i, radius_m, limit=40):
    best = cells[best_i]
    chosen = []
    for item, weight in zip(cells, weights):
        distance = _haversine(best["lat"], best["lon"], item["lat"], item["lon"])
        if distance <= radius_m + 1.0:
            chosen.append((distance, float(weight), item))
    chosen.sort(key=lambda row: (row[0], -row[1]))
    return [
        {"lat": float(item["lat"]), "lon": float(item["lon"]), "weight": weight}
        for _, weight, item in chosen[:limit]
    ]


def _other_modes(cells, costs, best_i, spacing_m, best_cost):
    margin = max(_ALT_MARGIN_HZ, 0.15 * best_cost)
    sep = 2.0 * spacing_m
    modes = []
    for index, cost in enumerate(costs):
        if index == best_i or not math.isfinite(cost) or cost - best_cost > margin:
            continue
        if not _is_local_min(index, cells, costs, spacing_m):
            continue
        if _haversine(cells[best_i]["lat"], cells[best_i]["lon"], cells[index]["lat"], cells[index]["lon"]) < sep:
            continue
        modes.append({"lat": cells[index]["lat"], "lon": cells[index]["lon"], "explained": int(cells[index]["explained"]), "cost": float(cost)})
    modes.sort(key=lambda item: item["cost"])
    return modes


def _is_local_min(index, cells, costs, spacing_m):
    here = costs[index]
    origin = cells[index]
    for other, cost in zip(cells, costs):
        if other is origin or not math.isfinite(cost):
            continue
        if _haversine(origin["lat"], origin["lon"], other["lat"], other["lon"]) <= spacing_m * 1.25 and cost + 0.05 < here:
            return False
    return True


def _bootstrap(cost_cube, shifts, cells, best_i, floor_m, spacing_m):
    if cost_cube is None:
        return None
    cube = np.asarray(cost_cube, dtype=float)
    if cube.ndim != 3 or cube.shape[1] < 2:
        return None
    count, events, n_shifts = cube.shape
    if count != len(cells) or n_shifts != len(shifts):
        return None
    rng = np.random.default_rng(0)
    draws = _BOOTSTRAP_DRAWS
    picks = rng.integers(0, events, size=(draws, events))
    total = np.zeros((draws, count, n_shifts), dtype=float)
    for event in range(events):
        total += np.transpose(cube[:, picks[:, event], :], (1, 0, 2))
    winners = np.argmin(total.reshape(draws, count * n_shifts), axis=1)
    cell_w = winners // n_shifts
    best = cells[best_i]
    dist = np.array([
        _haversine(best["lat"], best["lon"], cells[int(index)]["lat"], cells[int(index)]["lon"])
        for index in cell_w
    ])
    sep = 2.0 * spacing_m
    return {
        "radius_68_m": max(floor_m, float(np.percentile(dist, 68))),
        "radius_90_m": max(floor_m, float(np.percentile(dist, 90))),
        "stability": float(np.mean(dist <= sep)),
    }


def _leave_one_out(cost_cube, cells, best_i, spacing_m):
    cube = np.asarray(cost_cube, dtype=float)
    events = cube.shape[1]
    sep = 2.0 * spacing_m
    best = cells[best_i]
    moved = []
    for leave in range(events):
        keep = [index for index in range(events) if index != leave]
        total = cube[:, keep, :].sum(axis=1)
        flat = int(np.argmin(total))
        cell = flat // total.shape[1]
        if _haversine(best["lat"], best["lon"], cells[cell]["lat"], cells[cell]["lon"]) > sep:
            moved.append(leave)
    return moved


def _shift_spread(cost_cube, best_i, shifts):
    if cost_cube is None or shifts is None or len(shifts) < 2:
        return 0.0
    column = np.asarray(cost_cube[best_i], dtype=float).sum(axis=0)
    if not np.isfinite(column).any():
        return 0.0
    weights = _posterior(column)
    delta = np.abs(np.asarray(shifts, dtype=float) - float(shifts[int(np.argmin(column))]))
    order = np.argsort(delta)
    cumulative = np.cumsum(weights[order])
    index = min(int(np.searchsorted(cumulative, 0.68, side="left")), len(order) - 1)
    step = abs(float(shifts[1] - shifts[0]))
    return max(0.5 * step, float(delta[order[index]]))


def _haversine(lat1, lon1, lat2, lon2):
    radius = 6371000.0
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp, dl = math.radians(float(lat2) - float(lat1)), math.radians(float(lon2) - float(lon1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(max(0.0, a))))
