"""Colour palettes, expressed as gradient stops and baked into 256-entry LUTs."""

from __future__ import annotations

import numpy as np

_STOPS: dict[str, list[tuple[float, tuple[int, int, int]]]] = {
    "aurora": [
        (0.00, (0, 40, 90)),
        (0.30, (0, 190, 160)),
        (0.55, (80, 255, 120)),
        (0.78, (150, 120, 255)),
        (1.00, (0, 40, 90)),
    ],
    "ember": [
        (0.00, (30, 0, 0)),
        (0.35, (200, 30, 0)),
        (0.65, (255, 140, 0)),
        (0.85, (255, 230, 140)),
        (1.00, (30, 0, 0)),
    ],
    "ice": [
        (0.00, (0, 20, 60)),
        (0.40, (0, 140, 220)),
        (0.70, (140, 230, 255)),
        (1.00, (0, 20, 60)),
    ],
    "magma": [
        (0.00, (10, 0, 20)),
        (0.30, (120, 0, 90)),
        (0.60, (240, 60, 40)),
        (0.85, (255, 200, 60)),
        (1.00, (10, 0, 20)),
    ],
    "white": [(0.0, (255, 255, 255)), (1.0, (255, 255, 255))],
}


def _rainbow() -> np.ndarray:
    i = np.arange(256, dtype=np.float32) / 256.0
    # Simple HSV sweep at full saturation/value.
    h = i * 6.0
    x = (1.0 - np.abs(h % 2.0 - 1.0)) * 255.0
    c = np.full(256, 255.0, dtype=np.float32)
    z = np.zeros(256, dtype=np.float32)
    seg = h.astype(np.int32) % 6
    r = np.select([seg == 0, seg == 1, seg == 2, seg == 3, seg == 4, seg == 5], [c, x, z, z, x, c])
    g = np.select([seg == 0, seg == 1, seg == 2, seg == 3, seg == 4, seg == 5], [x, c, c, x, z, z])
    b = np.select([seg == 0, seg == 1, seg == 2, seg == 3, seg == 4, seg == 5], [z, z, x, c, c, x])
    return np.stack([r, g, b], axis=1).astype(np.uint8)


def build_lut(name: str) -> np.ndarray:
    """Return a uint8 (256, 3) RGB lookup table for the named palette."""
    if name == "rainbow":
        return _rainbow()
    stops = _STOPS.get(name)
    if stops is None:
        raise KeyError(f"unknown palette '{name}'; available: {', '.join(names())}")
    pos = np.array([s[0] for s in stops], dtype=np.float32)
    cols = np.array([s[1] for s in stops], dtype=np.float32)
    x = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    lut = np.stack([np.interp(x, pos, cols[:, c]) for c in range(3)], axis=1)
    return np.clip(lut, 0, 255).astype(np.uint8)


def names() -> list[str]:
    return sorted(list(_STOPS) + ["rainbow"])
