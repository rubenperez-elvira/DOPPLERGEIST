"""Acoustic Doppler geometry in SI units.

Observer and aircraft altitudes must be in the same vertical datum before they
are passed here. This module does not convert pressure altitude to ellipsoidal
height. Range rate is positive when the source recedes. The speed of sound is
an assumed constant, not a measured atmospheric profile.
"""

from __future__ import annotations

import math

import numpy as np

WGS84_A = 6378137.0
WGS84_E2 = 6.6943799901413165e-3
DEFAULT_C = 343.0
FT_TO_M = 0.3048
KT_TO_MPS = 0.514444


def ecef(lat_deg, lon_deg, height_m):
    lat = np.deg2rad(np.asarray(lat_deg, dtype=float))
    lon = np.deg2rad(np.asarray(lon_deg, dtype=float))
    height = np.asarray(height_m, dtype=float)
    lat, lon, height = np.broadcast_arrays(lat, lon, height)
    normal = WGS84_A / np.sqrt(1.0 - WGS84_E2 * np.sin(lat) ** 2)
    return np.stack(
        (
            (normal + height) * np.cos(lat) * np.cos(lon),
            (normal + height) * np.cos(lat) * np.sin(lon),
            (normal * (1.0 - WGS84_E2) + height) * np.sin(lat),
        ),
        axis=-1,
    )


def straight_doppler(t_receive, f0, speed, slant, cpa_receive, c=DEFAULT_C):
    """Retarded-time Doppler for a straight, constant-speed flyover.

    ``cpa_receive`` is the reception time of the sound emitted at the closest
    point of approach. ``slant`` is that closest 3-D distance in metres.
    ``speed`` is the source speed in metres per second and must stay below ``c``.
    """
    speed = float(speed)
    slant = float(slant)
    c = float(c)
    f0 = float(f0)
    if c <= 0 or slant <= 0 or f0 <= 0:
        raise ValueError("f0, slant y c tienen que ser positivos")
    if not 0 < speed < c:
        raise ValueError("la velocidad tiene que ser subsónica y positiva")
    beta = speed / c
    u = np.asarray(t_receive, dtype=float) - float(cpa_receive) + slant / c
    disc = (beta * u) ** 2 + (1.0 - beta**2) * (slant / c) ** 2
    tau = (u - np.sqrt(disc)) / (1.0 - beta**2)
    radial = speed**2 * tau / np.sqrt(slant**2 + (speed * tau) ** 2)
    return f0 * c / (c + radial)


def retarded_from_positions(emission_s, positions_m, observer_m, f0, c=DEFAULT_C):
    """Numerical emission-time Doppler, including propagation delay.

    Velocity comes from the position samples. A segment shorter than three
    samples, or a non-finite sample, is rejected. Reception time is
    ``emission + distance / c``. Frequency uses the radial velocity at emission.
    """
    emission = np.asarray(emission_s, dtype=float)
    position = np.asarray(positions_m, dtype=float)
    observer = np.asarray(observer_m, dtype=float)
    if emission.ndim != 1 or position.shape != (emission.size, 3) or observer.shape != (3,):
        raise ValueError("forma de trayectoria no válida")
    if emission.size < 3 or not np.all(np.diff(emission) > 0):
        raise ValueError("hacen falta al menos tres tiempos de emisión crecientes")
    if not np.isfinite(emission).all() or not np.isfinite(position).all() or not np.isfinite(observer).all():
        raise ValueError("hay valores no finitos en la trayectoria")
    if float(f0) <= 0 or float(c) <= 0:
        raise ValueError("f0 y c tienen que ser positivos")
    relative = position - observer
    distance = np.linalg.norm(relative, axis=1)
    if np.any(distance <= 1.0):
        raise ValueError("la trayectoria pasa a menos de 1 m del observador")
    velocity = np.gradient(position, emission, axis=0)
    speed = np.linalg.norm(velocity, axis=1)
    if np.any(speed >= c):
        raise ValueError("movimiento no subsónico en la trayectoria muestreada")
    radial = np.sum(velocity * relative, axis=1) / distance
    receive = emission + distance / float(c)
    frequency = float(f0) * float(c) / (float(c) + radial)
    return {
        "emission_s": emission,
        "receive_s": receive,
        "distance_m": distance,
        "radial_mps": radial,
        "speed_mps": speed,
        "frequency_hz": frequency,
    }


def nearest_approach(samples: dict) -> dict:
    index = int(np.argmin(samples["distance_m"]))
    return {
        "emission_s": float(samples["emission_s"][index]),
        "receive_s": float(samples["receive_s"][index]),
        "distance_m": float(samples["distance_m"][index]),
        "radial_mps": float(samples["radial_mps"][index]),
        "frequency_hz": float(samples["frequency_hz"][index]),
    }


def fit_straight_doppler(time_s, freq_hz, c=DEFAULT_C):
    """Least-squares straight-flyover fit. The scale ``f0`` is closed-form.

    The returned dispersion describes other grid points that fit nearly as well.
    It is not a statistical confidence interval.
    """
    time = np.asarray(time_s, dtype=float)
    freq = np.asarray(freq_hz, dtype=float)
    if time.shape != freq.shape or time.ndim != 1 or time.size < 8:
        raise ValueError("la pista es demasiado corta para ajustar")
    if not np.all(np.diff(time) > 0) or not np.isfinite(time).all() or not np.isfinite(freq).all():
        raise ValueError("tiempos o frecuencias no válidos")
    if np.any(freq <= 0):
        raise ValueError("la frecuencia observada tiene que ser positiva")

    slope = np.polyfit(time, freq, 1)
    linear = np.polyval(slope, time)
    linear_rmse = float(np.sqrt(np.mean((freq - linear) ** 2)))

    t_grid = np.linspace(float(time[0]), float(time[-1]), 21)
    speed_grid = np.geomspace(40.0, 280.0, 14)
    slant_grid = np.geomspace(60.0, 12000.0, 16)
    best = None
    near = []
    for t_cpa in t_grid:
        for speed in speed_grid:
            for slant in slant_grid:
                trial = _score(time, freq, float(speed), float(slant), float(t_cpa), c)
                if best is None or trial["rmse_hz"] < best["rmse_hz"]:
                    best = trial
    assert best is not None
    for _ in range(2):
        best = _refine(time, freq, best, c)
    threshold = best["rmse_hz"] * 1.25 + 0.05
    for t_cpa in np.linspace(best["cpa_receive_s"] - 1.5, best["cpa_receive_s"] + 1.5, 7):
        for speed in best["speed_mps"] * np.array([0.85, 1.0, 1.15]):
            for slant in best["slant_m"] * np.array([0.75, 1.0, 1.35]):
                if not 30.0 <= speed < c or not 40.0 <= slant <= 20000.0:
                    continue
                trial = _score(time, freq, float(speed), float(slant), float(t_cpa), c)
                if trial["rmse_hz"] <= threshold:
                    near.append(trial)
                if trial["rmse_hz"] < best["rmse_hz"]:
                    best = trial
    if not near:
        near = [best]
    best["linear_rmse_hz"] = linear_rmse
    best["duration_s"] = float(time[-1] - time[0])
    best["freq_min_hz"] = float(freq.min())
    best["freq_max_hz"] = float(freq.max())
    best["dispersion"] = {
        "note": "Dispersión de ajustes casi equivalentes. No es un intervalo de confianza.",
        "f0_hz": [float(min(item["f0_hz"] for item in near)), float(max(item["f0_hz"] for item in near))],
        "speed_mps": [float(min(item["speed_mps"] for item in near)), float(max(item["speed_mps"] for item in near))],
        "slant_m": [float(min(item["slant_m"] for item in near)), float(max(item["slant_m"] for item in near))],
        "cpa_receive_s": [
            float(min(item["cpa_receive_s"] for item in near)),
            float(max(item["cpa_receive_s"] for item in near)),
        ],
    }
    ss_tot = float(np.sum((freq - freq.mean()) ** 2))
    best["r2"] = float(1.0 - (best["rmse_hz"] ** 2 * freq.size) / ss_tot) if ss_tot > 0 else 0.0
    best["c_mps"] = float(c)
    best["model"] = "paso_recto_velocidad_constante_con_retardo"
    return best


def _score(time, freq, speed, slant, t_cpa, c):
    shape = straight_doppler(time, 1.0, speed, slant, t_cpa, c)
    denom = float(np.dot(shape, shape))
    f0 = float(np.dot(freq, shape) / denom)
    if not math.isfinite(f0) or f0 <= 0:
        rmse = 1e9
        predicted = np.zeros_like(freq)
        f0 = float("nan")
    else:
        predicted = f0 * shape
        rmse = float(np.sqrt(np.mean((freq - predicted) ** 2)))
    return {
        "f0_hz": f0,
        "speed_mps": speed,
        "slant_m": slant,
        "cpa_receive_s": t_cpa,
        "rmse_hz": rmse,
        "predicted_hz": predicted,
    }


def _refine(time, freq, best, c):
    current = best
    for scale_t, scale_v, scale_b in (
        (0.6, 1.12, 1.25),
        (0.25, 1.05, 1.1),
    ):
        for t_cpa in np.linspace(current["cpa_receive_s"] - scale_t, current["cpa_receive_s"] + scale_t, 7):
            for speed in current["speed_mps"] * np.geomspace(1 / scale_v, scale_v, 7):
                for slant in current["slant_m"] * np.geomspace(1 / scale_b, scale_b, 7):
                    if not 30.0 <= speed < c or not 40.0 <= slant <= 20000.0:
                        continue
                    trial = _score(time, freq, float(speed), float(slant), float(t_cpa), c)
                    if trial["rmse_hz"] < current["rmse_hz"]:
                        current = trial
    return current


def public_fit(fit: dict) -> dict:
    predicted = fit.get("predicted_hz")
    payload = {key: value for key, value in fit.items() if key != "predicted_hz"}
    if predicted is not None:
        step = max(1, int(len(predicted) / 200))
        payload["predicted_hz_preview"] = [float(value) for value in np.asarray(predicted)[::step]]
    for key in ("f0_hz", "speed_mps", "slant_m", "cpa_receive_s", "rmse_hz", "linear_rmse_hz", "r2", "duration_s"):
        if key in payload and isinstance(payload[key], float):
            payload[key] = float(payload[key])
    return payload
