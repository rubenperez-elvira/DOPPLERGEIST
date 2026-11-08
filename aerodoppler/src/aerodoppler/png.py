"""Small PNG writer so spectrograms do not need matplotlib."""

from __future__ import annotations

import struct
import zlib

import numpy as np


def spectrogram_png(power, width=360, height=180) -> bytes:
    image = _resize(np.asarray(power, dtype=np.float32), width, height)
    finite = image[np.isfinite(image)]
    if finite.size == 0:
        image = np.zeros((height, width), dtype=np.float32)
    else:
        low, high = np.percentile(finite, [5, 99.5])
        if high <= low:
            high = low + 1.0
        image = np.clip((image - low) / (high - low), 0.0, 1.0)
    rgb = _viridis(image)
    return _encode(rgb)


def _resize(grid, width, height):
    if grid.ndim != 2 or grid.size == 0:
        return np.zeros((height, width), dtype=np.float32)
    source_y = np.linspace(0, grid.shape[0] - 1, height)
    source_x = np.linspace(0, grid.shape[1] - 1, width)
    y0 = np.floor(source_y).astype(int)
    x0 = np.floor(source_x).astype(int)
    y1 = np.minimum(y0 + 1, grid.shape[0] - 1)
    x1 = np.minimum(x0 + 1, grid.shape[1] - 1)
    wy = (source_y - y0)[:, None]
    wx = source_x - x0
    top = grid[y0][:, x0] * (1 - wx) + grid[y0][:, x1] * wx
    bottom = grid[y1][:, x0] * (1 - wx) + grid[y1][:, x1] * wx
    return (top * (1 - wy) + bottom * wy).astype(np.float32)


def _viridis(values):
    stops = np.array(
        [
            [68, 1, 84],
            [59, 82, 139],
            [33, 145, 140],
            [94, 201, 98],
            [253, 231, 37],
        ],
        dtype=np.float32,
    )
    index = values * (len(stops) - 1)
    low = np.floor(index).astype(int)
    high = np.minimum(low + 1, len(stops) - 1)
    mix = (index - low)[..., None]
    return stops[low] * (1 - mix) + stops[high] * mix


def _encode(rgb) -> bytes:
    height, width = rgb.shape[:2]
    raw = np.clip(np.round(rgb), 0, 255).astype(np.uint8)
    rows = b"".join(b"\x00" + raw[y].tobytes() for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b"")
