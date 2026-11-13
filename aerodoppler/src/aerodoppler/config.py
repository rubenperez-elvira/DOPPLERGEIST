"""Runtime limits. Secrets come only from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass

UPLOAD_HARD_BYTES = 16 * 1024 * 1024
DURATION_HARD_S = 120.0
RADIUS_HARD_KM = 150.0
WINDOW_HARD_MIN = 360.0
MIN_TOKEN_LENGTH = 20


def _float(name: str, default: float, upper: float, lower: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        value = default
    else:
        value = float(raw)
    if not lower <= value <= upper:
        raise ValueError(f"{name} fuera de [{lower}, {upper}]")
    return value


def _int(name: str, default: int, upper: int, lower: int = 1) -> int:
    raw = os.environ.get(name)
    value = default if raw is None or raw.strip() == "" else int(raw)
    if not lower <= value <= upper:
        raise ValueError(f"{name} fuera de [{lower}, {upper}]")
    return value


@dataclass(frozen=True)
class Settings:
    access_token: str | None
    cookie_secure: bool
    max_upload_bytes: int
    max_duration_s: float
    sample_rate: int
    max_radius_km: float
    max_window_min: float
    ffmpeg_timeout_s: float
    history_timeout_s: float
    max_history_pages: int
    max_flights: int
    lookback_h: float
    analysis_timeout_s: float

    @property
    def auth_configured(self) -> bool:
        return self.access_token is not None


def load_settings() -> Settings:
    raw = os.environ.get("AERDOPPLER_ACCESS_TOKEN", "")
    token = raw.strip()
    configured = token if len(token) >= MIN_TOKEN_LENGTH else None
    secure_raw = os.environ.get("AERDOPPLER_COOKIE_SECURE", "auto").strip().lower()
    upload = _int("AERDOPPLER_MAX_UPLOAD_BYTES", 8 * 1024 * 1024, UPLOAD_HARD_BYTES, 64 * 1024)
    return Settings(
        access_token=configured,
        cookie_secure=secure_raw in {"1", "true", "yes", "on"},
        max_upload_bytes=upload,
        max_duration_s=_float("AERDOPPLER_MAX_DURATION_S", 90.0, DURATION_HARD_S, 5.0),
        sample_rate=16000,
        max_radius_km=_float("AERDOPPLER_MAX_RADIUS_KM", 120.0, RADIUS_HARD_KM, 1.0),
        max_window_min=_float("AERDOPPLER_MAX_WINDOW_MIN", 360.0, WINDOW_HARD_MIN, 1.0),
        ffmpeg_timeout_s=_float("AERDOPPLER_FFMPEG_TIMEOUT_S", 25.0, 60.0, 5.0),
        history_timeout_s=_float("AERDOPPLER_HISTORY_TIMEOUT_S", 25.0, 40.0, 5.0),
        max_history_pages=_int("AERDOPPLER_MAX_HISTORY_PAGES", 3, 6, 1),
        max_flights=_int("AERDOPPLER_MAX_FLIGHTS", 40, 80, 1),
        lookback_h=_float("AERDOPPLER_LOOKBACK_H", 72.0, 96.0, 1.0),
        analysis_timeout_s=_float("AERDOPPLER_ANALYSIS_TIMEOUT_S", 40.0, 90.0, 5.0),
    )
