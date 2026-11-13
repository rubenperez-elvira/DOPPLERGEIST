"""Compare an observed ridge with real trajectory samples.

Several compatible tracks stay visible. A low residual is a geometric match of
the Doppler model, not a unique identification of an airframe.
"""

from __future__ import annotations

from .physics import DEFAULT_C, ecef, nearest_approach, retarded_from_positions

import numpy as np


def compare_tracks(observer: dict, flights: list[dict], ridge: dict | None, recording_start_unix: float | None, f0_hz: float | None, c: float = DEFAULT_C, window: tuple[float, float] | None = None) -> dict:
    if not -90 <= float(observer["lat"]) <= 90 or not -180 <= float(observer["lon"]) <= 180:
        raise ValueError("Coordenadas del observador no válidas.")
    altitude = observer.get("alt_m")
    if altitude is None or not np.isfinite(float(altitude)):
        raise ValueError("Falta la altitud del observador en metros y en el mismo criterio vertical que las trayectorias.")
    observer_xyz = ecef(observer["lat"], observer["lon"], float(altitude)).reshape(3)
    reports = []
    for flight in flights[:40]:
        reports.append(_one(flight, observer_xyz, ridge, recording_start_unix, f0_hz, c, window))
    ranked = [item for item in reports if item.get("rmse_hz") is not None and item.get("acoustic_eligible")]
    ranked.sort(key=lambda item: item["rmse_hz"])
    ambiguous = False
    if len(ranked) >= 2 and ranked[0]["rmse_hz"] > 0 and ranked[1]["rmse_hz"] / max(ranked[0]["rmse_hz"], 1e-6) < 1.2:
        ambiguous = True
    if ranked:
        status = "ambiguous" if ambiguous else "ranked_not_unique"
        message = (
            "Hay un orden por residuo del modelo Doppler. No es una identificación única."
            if not ambiguous
            else "Los dos mejores residuos son parecidos. El resultado es ambiguo y no identifica un vuelo."
        )
    elif ridge is None:
        status = "geometry_only"
        message = "No hay pista de audio. Se calculan distancia, velocidad radial y curva teórica, sin ajuste."
    elif recording_start_unix is None:
        status = "needs_recording_clock"
        message = "Falta la hora de inicio de la grabación. Sin ese reloj no se alinea el audio con las trayectorias."
    else:
        status = "no_eligible_track"
        message = "Ningún vuelo elegible produjo un solape acústico puntuable. Los reactores y los tipos desconocidos no entran en el ranking."
    return {
        "status": status,
        "message": message,
        "ambiguous": ambiguous,
        "unique_identification": False,
        "c_mps": c,
        "observer": {"lat": observer["lat"], "lon": observer["lon"], "alt_m": float(altitude), "altitude_note": observer.get("altitude_note")},
        "flights": reports,
        "datum_note": (
            "La distancia 3D usa coordenadas ECEF WGS84. La altitud de la aeronave llega como altitud de presión "
            "más la corrección QNH de adsb.aero cuando existe, convertida a metros. La altitud del observador es "
            "la que indica el usuario. Si una es ortométrica y la otra de presión corregida, el criterio vertical "
            "no es idéntico; la diferencia de geoide queda dentro de la incertidumbre."
        ),
    }


def _one(flight, observer_xyz, ridge, recording_start_unix, f0_hz, c, window=None):
    points = flight.get("points") or []
    if window is not None:
        start, end = window
        points = [point for point in points if start <= float(point.get("t", -1)) <= end]
        if not points:
            return {**base, "geometry_status": "outside_window", "message": "Ningún vértice cae dentro de la ventana pedida."}
    base = {
        "flight_id": flight.get("flight_id"),
        "icao24": flight.get("icao24"),
        "callsign": flight.get("callsign"),
        "icao_type": flight.get("icao_type"),
        "engine_class": flight.get("engine_class"),
        "acoustic_eligible": bool(flight.get("acoustic_eligible")),
        "identification": False,
    }
    closest = _closest_vertex(points, observer_xyz, c)
    if closest is None:
        return {**base, "geometry_status": "insufficient_track", "message": "La trayectoria no tiene posiciones finitas."}
    segments = _segments(points)
    curves = []
    curve_error = None
    try:
        for times, positions in segments:
            source_f0 = float(f0_hz) if f0_hz else 1.0
            samples = retarded_from_positions(times, positions, observer_xyz, source_f0, c)
            curves.append((samples, nearest_approach(samples)))
    except ValueError as error:
        curve_error = str(error)
        curves = []
    preview = _preview(curves)
    result = {
        **base,
        "geometry_status": "ok" if curves else "distance_only",
        "closest_m": closest["distance_m"],
        "closest_emission_unix": closest["emission_s"],
        "closest_receive_unix": closest["receive_s"],
        "propagation_delay_s": closest["delay_s"],
        "radial_at_closest_mps": closest["radial_mps"],
        "closest_is_sampled_vertex": True,
        "curve_preview": preview,
        "f0_mode": "absolute" if f0_hz else "normalized_shape_only",
        "curve_note": curve_error or (
            "La curva usa tramos con al menos tres puntos y huecos de cobertura de más de 70 s no se interpolan."
            if curves else "No hay un tramo continuo bastante largo para la curva Doppler."
        ),
    }
    if not flight.get("acoustic_eligible"):
        result["rank_status"] = "excluded_from_acoustic_rank"
        result["message"] = (
            "Queda fuera del ranking acústico por ser reactor o de tipo no confirmado. "
            + ("Hay curva teórica por tramos." if curves else "Solo se informa la distancia al vértice más cercano.")
        )
        return result
    if ridge is None or recording_start_unix is None:
        result["rank_status"] = "not_scored"
        result["message"] = "Geometría calculada. Falta audio o el reloj de la grabación para puntuar la compatibilidad."
        return result
    observed_t = np.asarray(ridge["time_s"], dtype=float) + float(recording_start_unix)
    observed_f = np.asarray(ridge["frequency_hz"], dtype=float)
    score = _score_curve(curves, observed_t, observed_f, f0_hz)
    if score is None:
        result["rank_status"] = "no_time_overlap"
        result["message"] = "La curva teórica no solapa al menos 4 s con la pista grabada."
        return result
    result.update(score)
    result["rank_status"] = "scored"
    result["message"] = "Residuo entre la pista y la curva con retardo. No identifica el vuelo por sí solo."
    return result


def _closest_vertex(points, observer_xyz, c):
    ordered = []
    for point in points or []:
        try:
            stamp = float(point["t"])
            lat = float(point["lat"])
            lon = float(point["lon"])
            alt = float(point["alt_m"])
        except (KeyError, TypeError, ValueError):
            continue
        if np.isfinite([stamp, lat, lon, alt]).all():
            ordered.append({"t": stamp, "lat": lat, "lon": lon, "alt_m": alt})
    if not ordered:
        return None
    ordered.sort(key=lambda item: item["t"])
    positions = ecef([item["lat"] for item in ordered], [item["lon"] for item in ordered], [item["alt_m"] for item in ordered])
    distance = np.linalg.norm(positions - observer_xyz, axis=1)
    index = int(np.argmin(distance))
    radial = None
    if 0 < index < len(ordered) - 1:
        left = ordered[index]["t"] - ordered[index - 1]["t"]
        right = ordered[index + 1]["t"] - ordered[index]["t"]
        if 0 < left <= 70.0 and 0 < right <= 70.0:
            velocity = (positions[index + 1] - positions[index - 1]) / (left + right)
            radial = float(np.dot(velocity, positions[index] - observer_xyz) / distance[index])
    return {
        "distance_m": float(distance[index]),
        "emission_s": ordered[index]["t"],
        "receive_s": ordered[index]["t"] + float(distance[index]) / c,
        "delay_s": float(distance[index]) / c,
        "radial_mps": radial,
    }


def _segments(points):
    ordered = sorted(points, key=lambda item: float(item["t"]))
    groups = []
    current = []
    previous = None
    for point in ordered:
        stamp = float(point["t"])
        if previous is not None and stamp - previous > 70.0:
            if len(current) >= 3:
                groups.append(current)
            current = []
        current.append(point)
        previous = stamp
    if len(current) >= 3:
        groups.append(current)
    segments = []
    for group in groups:
        times = np.array([float(point["t"]) for point in group])
        if np.any(np.diff(times) <= 0):
            continue
        positions = ecef(
            [point["lat"] for point in group],
            [point["lon"] for point in group],
            [point["alt_m"] for point in group],
        )
        segments.append((times, positions))
    return segments


def _preview(curves):
    rows = []
    for samples, _approach in curves:
        step = max(1, int(len(samples["receive_s"]) / 80))
        for index in range(0, len(samples["receive_s"]), step):
            rows.append(
                {
                    "receive_unix": float(samples["receive_s"][index]),
                    "emission_unix": float(samples["emission_s"][index]),
                    "distance_m": float(samples["distance_m"][index]),
                    "radial_mps": float(samples["radial_mps"][index]),
                    "frequency_hz": float(samples["frequency_hz"][index]),
                }
            )
    return rows


def _score_curve(curves, observed_t, observed_f, f0_hz):
    best = None
    for shift in np.linspace(-5.0, 5.0, 21):
        times = observed_t + shift
        observed_parts = []
        theory_parts = []
        covered = []
        for samples, _approach in curves:
            receive = samples["receive_s"]
            if receive.size < 2 or np.any(np.diff(receive) <= 0):
                continue
            mask = (times >= receive[0]) & (times <= receive[-1])
            if int(np.count_nonzero(mask)) < 4:
                continue
            observed_parts.append(observed_f[mask])
            theory_parts.append(np.interp(times[mask], receive, samples["frequency_hz"]))
            covered.append(times[mask])
        if not observed_parts:
            continue
        observed = np.concatenate(observed_parts)
        theory = np.concatenate(theory_parts)
        span = np.concatenate(covered)
        if observed.size != theory.size or float(span.max() - span.min()) < 4.0:
            continue
        if f0_hz:
            residual = observed - theory
        else:
            residual = (observed / np.median(observed) - theory / np.median(theory)) * float(np.median(observed))
        rmse = float(np.sqrt(np.mean(residual**2)))
        ss_tot = float(np.sum((observed - np.mean(observed)) ** 2))
        r2 = float(1.0 - np.sum(residual**2) / ss_tot) if ss_tot > 0 else 0.0
        trial = {"rmse_hz": rmse, "r2": r2, "clock_shift_s": float(shift), "overlap_samples": int(observed.size)}
        if best is None or trial["rmse_hz"] < best["rmse_hz"]:
            best = trial
    return best


def compare_csv(result: dict) -> str:
    rows = ["callsign,icao24,engine_class,eligible,closest_km,delay_s,rmse_hz,r2,clock_shift_s,rank_status"]
    for flight in result.get("flights") or []:
        closest = flight.get("closest_m")
        rows.append(
            ",".join(
                [
                    _cell(flight.get("callsign")),
                    _cell(flight.get("icao24")),
                    _cell(flight.get("engine_class")),
                    "si" if flight.get("acoustic_eligible") else "no",
                    "" if closest is None else f"{closest / 1000:.3f}",
                    "" if flight.get("propagation_delay_s") is None else f"{flight['propagation_delay_s']:.2f}",
                    "" if flight.get("rmse_hz") is None else f"{flight['rmse_hz']:.3f}",
                    "" if flight.get("r2") is None else f"{flight['r2']:.4f}",
                    "" if flight.get("clock_shift_s") is None else f"{flight['clock_shift_s']:.2f}",
                    _cell(flight.get("rank_status")),
                ]
            )
        )
    return "\n".join(rows) + "\n"


def _cell(value) -> str:
    text = "" if value is None else str(value)
    if any(character in text for character in ",\"\n"):
        return '"' + text.replace('"', '""') + '"'
    return text
