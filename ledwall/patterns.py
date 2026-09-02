"""Full-colour test frames for wiring verification.

These deliberately encode position, so a wrong serpentine phase or a swapped
output is obvious on the wall rather than something you squint at.
"""

from __future__ import annotations

import numpy as np

PATTERNS = ("solid", "gradient", "checker", "rows", "columns", "border", "corner", "chase", "index")


def _rgb(h: int, w: int) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def make(name: str, width: int, height: int, phase: float = 0.0, colour=(255, 255, 255)) -> np.ndarray:
    """Return an (height, width, 3) uint8 test frame."""
    h, w = height, width
    ys, xs = np.mgrid[0:h, 0:w]
    img = _rgb(h, w)

    if name == "solid":
        img[:] = np.array(colour, dtype=np.uint8)
    elif name == "gradient":
        # Red rises left->right, green top->bottom: tells you both axes at once.
        img[..., 0] = (xs * 255 // max(w - 1, 1)).astype(np.uint8)
        img[..., 1] = (ys * 255 // max(h - 1, 1)).astype(np.uint8)
    elif name == "checker":
        c = (((xs // 5) + (ys // 5)) % 2).astype(np.uint8) * 255
        img[..., 0] = c
        img[..., 1] = c
        img[..., 2] = c
    elif name == "rows":
        # Every 5th row white, others dim: counts rows without ambiguity.
        img[(ys % 5) == 0] = (255, 255, 255)
        img[(ys % 5) != 0] = (0, 0, 20)
    elif name == "columns":
        img[:, ::5] = (255, 255, 255)
    elif name == "border":
        img[0, :] = (255, 0, 0)       # top edge red
        img[-1, :] = (0, 0, 255)      # bottom edge blue
        img[:, 0] = (0, 255, 0)       # left edge green
        img[:, -1] = (255, 255, 0)    # right edge yellow
    elif name == "corner":
        # Only the true top-left 5x5 lights: confirms origin and orientation.
        img[0:5, 0:5] = (255, 255, 255)
        img[0:2, 0:2] = (255, 0, 0)
    elif name == "chase":
        pos = int(phase) % (w * h)
        y, x = divmod(pos, w)
        img[y, x] = (255, 255, 255)
        for back in range(1, 12):
            p = pos - back
            if p < 0:
                break
            yy, xx = divmod(p, w)
            v = int(255 * (1 - back / 12))
            img[yy, xx] = (v, v // 3, 0)
    elif name == "index":
        # Row index in binary on the low bits of blue; niche but handy.
        img[..., 2] = (ys % 8) * 32
        img[..., 0] = (xs % 8) * 32
    else:
        raise KeyError(f"unknown pattern '{name}'; available: {', '.join(PATTERNS)}")
    return img
