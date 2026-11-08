"""Decode short recordings and measure tonal ridges.

A descending tone is not treated as an aircraft. Acceptance means only that a
straight constant-speed Doppler curve fits the measured ridge better than a
straight line, with enough duration, signal and curvature.
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

import numpy as np

from .physics import fit_straight_doppler, public_fit
from .png import spectrogram_png

ALLOWED_FORMATS = {"wav", "mp3", "mov", "mp4", "m4a", "isom", "3gp", "3g2", "mj2"}
ALLOWED_CODECS = {"pcm_s16le", "pcm_s24le", "pcm_f32le", "pcm_f64le", "pcm_s32le", "mp3", "aac", "alac", "mp4a"}


class AudioRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def sniff_media(data: bytes) -> str | None:
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:3] == b"ID3":
        return "mp3"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "mp4"
    return None


def ffmpeg_decode_command(ffmpeg: str, path: Path, sample_rate: int, max_duration_s: float) -> list[str]:
    return [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-probesize",
        "5000000",
        "-analyzeduration",
        "5000000",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-t",
        f"{max_duration_s + 0.05:.3f}",
        "-f",
        "f32le",
        "-acodec",
        "pcm_f32le",
        "pipe:1",
    ]


def ffprobe_command(ffprobe: str, path: Path) -> list[str]:
    return [
        ffprobe,
        "-v",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-select_streams",
        "a:0",
        str(path),
    ]


def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, check=False, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise AudioRejected("tiempo_agotado", "El análisis de audio superó el tiempo permitido.") from error
    except FileNotFoundError as error:
        raise AudioRejected("ffmpeg_ausente", "ffmpeg o ffprobe no están disponibles en el servidor.") from error


def probe_audio(ffprobe: str, path: Path, timeout: float) -> dict:
    import json

    result = _run(ffprobe_command(ffprobe, path), timeout)
    if result.returncode != 0 or not result.stdout:
        raise AudioRejected("formato_no_valido", "No se pudo leer un flujo de audio compatible.")
    try:
        payload = json.loads(result.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as error:
        raise AudioRejected("formato_no_valido", "ffprobe no devolvió un informe utilizable.") from error
    formats = {part.strip().lower() for part in str(payload.get("format", {}).get("format_name", "")).split(",")}
    if not formats & ALLOWED_FORMATS:
        raise AudioRejected("formato_no_valido", "El contenedor no es WAV, MP3, M4A ni MP4 compatible.")
    streams = payload.get("streams") or []
    if not streams or streams[0].get("codec_type") != "audio":
        raise AudioRejected("sin_audio", "El archivo no contiene un flujo de audio.")
    codec = str(streams[0].get("codec_name", "")).lower()
    if codec not in ALLOWED_CODECS:
        raise AudioRejected("codec_no_valido", f"El códec de audio {codec or 'desconocido'} no está admitido.")
    duration = streams[0].get("duration") or payload.get("format", {}).get("duration")
    return {"formats": sorted(formats), "codec": codec, "duration_s": None if duration is None else float(duration)}


def decode_audio(ffmpeg: str, ffprobe: str, path: Path, sample_rate: int, max_duration_s: float, timeout: float) -> np.ndarray:
    info = probe_audio(ffprobe, path, timeout)
    if info["duration_s"] is not None and info["duration_s"] > max_duration_s + 0.25:
        raise AudioRejected(
            "duracion_excedida",
            f"El audio dura {info['duration_s']:.1f} s y el máximo es {max_duration_s:.0f} s.",
        )
    result = _run(ffmpeg_decode_command(ffmpeg, path, sample_rate, max_duration_s), timeout)
    if result.returncode != 0 or not result.stdout:
        raise AudioRejected("extraccion_fallida", "ffmpeg no pudo extraer audio de este archivo.")
    samples = np.frombuffer(result.stdout, dtype="<f4").astype(np.float32, copy=True)
    if samples.size == 0 or not np.isfinite(samples).all():
        raise AudioRejected("audio_no_utilizable", "El audio extraído está vacío o contiene valores no finitos.")
    limit = int((max_duration_s + 0.2) * sample_rate)
    if samples.size > limit:
        raise AudioRejected("duracion_excedida", f"El audio supera {max_duration_s:.0f} s después de extraerlo.")
    return samples


def analyze_samples(samples: np.ndarray, sample_rate: int, c: float = 343.0) -> dict:
    samples = np.asarray(samples, dtype=np.float32)
    duration = float(samples.size / sample_rate)
    rms = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    base = {
        "sample_rate_hz": sample_rate,
        "duration_s": duration,
        "rms": rms,
        "peak": peak,
        "aircraft_identification": False,
        "interpretation": "Una frecuencia descendente no se considera por sí sola una aeronave.",
    }
    if duration < 2.0:
        return _reject(base, ["duracion_insuficiente"], "El audio dura menos de 2 s.")
    if rms < 2e-3 or peak < 1e-2:
        return _reject(base, ["silencio"], "El audio es silencio o está por debajo del nivel utilizable.")

    power, times, freqs = _spectrogram(samples, sample_rate)
    ridge = _track_ridge(power, times, freqs)
    band = (freqs >= 40.0) & (freqs <= 1500.0)
    png = spectrogram_png(power[:, band].T if band.any() else power.T)
    base["spectrogram_png_base64"] = __import__("base64").b64encode(png).decode("ascii")
    base["spectrogram_note"] = "Espectrograma de potencia entre 40 y 1500 Hz. El color no es una probabilidad."
    if ridge is None:
        return _reject(base, ["sin_pista_tonal"], "No hay una pista tonal continua por encima del fondo.")

    base["ridge"] = {
        "time_s": [float(value) for value in ridge["time"]],
        "frequency_hz": [float(value) for value in ridge["frequency"]],
        "snr_db": float(ridge["snr_db"]),
        "duration_s": float(ridge["time"][-1] - ridge["time"][0]),
        "freq_span_hz": float(np.ptp(ridge["frequency"])),
        "tracked_fraction": float(ridge["tracked_fraction"]),
    }
    reasons = []
    if base["ridge"]["duration_s"] < 4.0:
        reasons.append("pista_corta")
    if base["ridge"]["snr_db"] < 8.0:
        reasons.append("pista_poco_clara")
    if base["ridge"]["freq_span_hz"] < 6.0:
        reasons.append("variacion_de_frecuencia_insuficiente")
    if base["ridge"]["tracked_fraction"] < 0.55:
        reasons.append("seguimiento_incompleto")
    try:
        fit = fit_straight_doppler(ridge["time"], ridge["frequency"], c=c)
    except ValueError:
        reasons.append("ajuste_no_calculable")
        return _reject(base, reasons, "La pista no admite un ajuste Doppler estable.")
    base["fit"] = public_fit(fit)
    if fit["r2"] < 0.8:
        reasons.append("r2_bajo")
    if fit["rmse_hz"] > 8.0:
        reasons.append("rmse_alto")
    if not (fit["linear_rmse_hz"] > 0 and fit["rmse_hz"] < fit["linear_rmse_hz"] * 0.75):
        reasons.append("no_se_distingue_de_un_descenso_lineal")
    if reasons:
        return _reject(
            base,
            reasons,
            "La pista no se acepta: no basta un descenso de frecuencia ni un ajuste débil.",
        )
    base["status"] = "accepted"
    base["reasons"] = []
    base["message"] = (
        "La pista se ajusta a un paso recto a velocidad constante con retardo acústico. "
        "Eso no identifica una aeronave ni excluye otras fuentes."
    )
    return base


def _reject(base: dict, reasons: list[str], message: str) -> dict:
    base["status"] = "rejected"
    base["reasons"] = reasons
    base["message"] = message
    return base


def _spectrogram(samples: np.ndarray, sample_rate: int):
    n_fft = 1024
    hop = 256
    audio = np.ascontiguousarray(samples, dtype=np.float32)
    if audio.size < n_fft:
        raise AudioRejected("duracion_insuficiente", "El audio es más corto que una ventana de análisis.")
    count = 1 + (audio.size - n_fft) // hop
    frames = np.lib.stride_tricks.as_strided(
        audio,
        shape=(count, n_fft),
        strides=(audio.strides[0] * hop, audio.strides[0]),
    )
    window = np.hanning(n_fft).astype(np.float32)
    spectrum = np.fft.rfft(frames * window, axis=1)
    power = (np.abs(spectrum) ** 2).astype(np.float32)
    times = (np.arange(count) * hop + n_fft / 2) / sample_rate
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    return power, times, freqs


def _track_ridge(power, times, freqs):
    band = (freqs >= 40.0) & (freqs <= 1500.0)
    band_index = np.flatnonzero(band)
    if band_index.size < 8:
        return None
    view = power[:, band_index]
    floor = np.median(view, axis=1) + 1e-12
    candidates = []
    for row, level in zip(view, floor):
        peaks = []
        for index in range(1, row.size - 1):
            if row[index] >= row[index - 1] and row[index] >= row[index + 1] and row[index] > level * 4.0:
                refined = _parabolic(row, index)
                peaks.append((float(freqs[band_index[0]] + refined * (freqs[1] - freqs[0])), float(row[index] / level)))
        peaks.sort(key=lambda item: item[1], reverse=True)
        candidates.append(peaks[:4])
    present = sum(bool(item) for item in candidates)
    if present < max(8, int(0.25 * len(candidates))):
        return None
    node_count = [1 + len(item) for item in candidates]
    offsets = np.cumsum([0, *node_count[:-1]])
    total = int(sum(node_count))
    frequency = np.zeros(total)
    strength = np.zeros(total)
    skip = np.zeros(total, dtype=bool)
    for frame, peaks in enumerate(candidates):
        base = offsets[frame]
        skip[base] = True
        for order, (freq, snr) in enumerate(peaks):
            frequency[base + 1 + order] = freq
            strength[base + 1 + order] = snr
    cost = np.full(total, np.inf)
    previous = np.full(total, -1, dtype=int)
    cost[offsets[0] : offsets[0] + node_count[0]] = 0.0
    for frame in range(1, len(candidates)):
        start, stop = offsets[frame - 1], offsets[frame]
        now = offsets[frame]
        for local in range(node_count[frame]):
            best_cost, best_from = math.inf, -1
            dest = now + local
            for source in range(start, stop):
                if not math.isfinite(cost[source]):
                    continue
                penalty = 0.15 if skip[dest] or skip[source] else abs(frequency[dest] - frequency[source]) / 12.0
                if not skip[dest] and not skip[source] and abs(frequency[dest] - frequency[source]) > 30.0:
                    penalty += 8.0
                penalty -= 0.02 * math.log(max(strength[dest], 1.0))
                proposal = cost[source] + penalty
                if proposal < best_cost:
                    best_cost, best_from = proposal, source
            cost[dest] = best_cost
            previous[dest] = best_from
    end = int(np.argmin(cost[offsets[-1] : offsets[-1] + node_count[-1]])) + offsets[-1]
    path = []
    cursor = end
    while cursor >= 0:
        path.append(cursor)
        cursor = int(previous[cursor])
    path.reverse()
    chosen_t = []
    chosen_f = []
    chosen_s = []
    for node in path:
        if not skip[node]:
            chosen_t.append(times[_frame_of(node, offsets)])
            chosen_f.append(frequency[node])
            chosen_s.append(strength[node])
    if len(chosen_f) < 8:
        return None
    tracked = np.array(chosen_f, dtype=float)
    smooth = _median_smooth(tracked, 5)
    snr_db = 10.0 * math.log10(float(np.median(chosen_s)) + 1e-12)
    return {
        "time": np.array(chosen_t, dtype=float),
        "frequency": smooth,
        "snr_db": snr_db,
        "tracked_fraction": len(chosen_f) / len(candidates),
    }


def _frame_of(node: int, offsets: np.ndarray) -> int:
    return int(np.searchsorted(offsets, node, side="right") - 1)


def _parabolic(row, index: int) -> float:
    left, center, right = float(row[index - 1]), float(row[index]), float(row[index + 1])
    denom = left - 2.0 * center + right
    if denom == 0.0:
        return float(index)
    delta = 0.5 * (left - right) / denom
    return float(index) + float(np.clip(delta, -0.5, 0.5))


def _median_smooth(values: np.ndarray, width: int) -> np.ndarray:
    if width < 3 or values.size < width:
        return values
    pad = width // 2
    padded = np.pad(values, pad, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, width)
    return np.median(windows, axis=1)


def ridge_csv(result: dict) -> str:
    ridge = result.get("ridge")
    if not ridge:
        return "time_s,frequency_hz\n"
    rows = ["time_s,frequency_hz"]
    for time, freq in zip(ridge["time_s"], ridge["frequency_hz"]):
        rows.append(f"{time:.4f},{freq:.4f}")
    return "\n".join(rows) + "\n"
