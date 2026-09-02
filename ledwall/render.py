"""Turn a person mask into the byte frame the wall expects.

Colour maths happens in linear 0..1 float, then the RGB->RGBW split, then
brightness, then gamma.  Doing gamma last matters: LEDs are perceptually
non-linear and gamma-correcting before the white split would skew hues.
"""

from __future__ import annotations

import numpy as np

from .config import Config
from .palettes import build_lut


def gamma_lut(gamma: float, brightness: float = 1.0) -> np.ndarray:
    """uint8 LUT mapping linear 0..255 to gamma-corrected 0..255."""
    x = np.linspace(0.0, 1.0, 256, dtype=np.float64)
    y = np.power(np.clip(x * brightness, 0.0, 1.0), gamma)
    return np.clip(np.rint(y * 255.0), 0, 255).astype(np.uint8)


def rgb_to_rgbw(rgb: np.ndarray, mode: str, white_gain: float = 1.0) -> np.ndarray:
    """Split linear float RGB (..., 3) in 0..1 into RGBW (..., 4).

    ``accurate`` moves the achromatic part of the colour onto the dedicated
    white LED and subtracts it from RGB, which keeps the hue and is far more
    efficient than making white out of three coloured dies.
    """
    if mode == "none":
        w = np.zeros(rgb.shape[:-1] + (1,), dtype=rgb.dtype)
        return np.concatenate([rgb, w], axis=-1)
    w = rgb.min(axis=-1, keepdims=True) * float(white_gain)
    w = np.clip(w, 0.0, 1.0)
    if mode == "min":
        return np.concatenate([rgb, w], axis=-1)
    return np.concatenate([np.clip(rgb - w, 0.0, 1.0), w], axis=-1)


class Renderer:
    """Stateful renderer: mask -> flat uint8 frame in grid order."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.w = cfg.grid.width
        self.h = cfg.grid.height
        self.channels = 4 if cfg.output.rgbw else 3
        self.set_palette(cfg.render.palette)
        self.brightness = cfg.output.brightness
        self._lut = gamma_lut(cfg.output.gamma)
        self._trail = np.zeros((self.h, self.w), dtype=np.float32)
        # Spatial term of the colour field, computed once.
        ys, xs = np.mgrid[0 : self.h, 0 : self.w].astype(np.float32)
        self._field = (xs / max(self.w - 1, 1)) * 0.6 + (ys / max(self.h - 1, 1)) * 0.25

    def set_palette(self, name: str) -> None:
        self._lut_rgb = build_lut(name).astype(np.float32) / 255.0
        self.palette = name

    def _colour_field(self, t: float) -> np.ndarray:
        cycle = max(self.cfg.render.cycle_seconds, 0.001)
        idx = (self._field + t / cycle) % 1.0
        i = (idx * 255.0).astype(np.uint8)
        return self._lut_rgb[i]  # (h, w, 3) float 0..1

    def _shape(self, mask: np.ndarray) -> np.ndarray:
        mode = self.cfg.render.mode
        if mode == "fill":
            return mask
        if mode == "outline":
            # Morphological gradient without needing OpenCV here.
            p = np.pad(mask, 1, mode="edge")
            mx = np.maximum.reduce([p[:-2, 1:-1], p[2:, 1:-1], p[1:-1, :-2], p[1:-1, 2:], mask])
            mn = np.minimum.reduce([p[:-2, 1:-1], p[2:, 1:-1], p[1:-1, :-2], p[1:-1, 2:], mask])
            return np.clip(mx - mn, 0.0, 1.0)
        # glow: cheap separable box blur, keeping the solid body at full value
        k = np.array([0.25, 0.5, 0.25], dtype=np.float32)
        blur = mask
        for _ in range(2):
            p = np.pad(blur, ((0, 0), (1, 1)), mode="edge")
            blur = p[:, :-2] * k[0] + p[:, 1:-1] * k[1] + p[:, 2:] * k[2]
            p = np.pad(blur, ((1, 1), (0, 0)), mode="edge")
            blur = p[:-2, :] * k[0] + p[1:-1, :] * k[1] + p[2:, :] * k[2]
        return np.clip(np.maximum(mask, blur * 1.2), 0.0, 1.0)

    def render(self, mask: np.ndarray, t: float) -> np.ndarray:
        """Return uint8 (h*w, channels) ready to hand to the wiring map."""
        mask = np.clip(mask.astype(np.float32), 0.0, 1.0)
        trail = self.cfg.render.trail
        if trail > 0.0:
            self._trail = np.maximum(self._trail * trail, mask)
            mask = self._trail
        intensity = self._shape(mask)

        bg = self.cfg.render.background
        if bg == "dim":
            intensity = np.maximum(intensity, self.cfg.render.background_level)
        elif bg == "idle":
            # Slow diagonal wave, only visible where the body isn't.
            wave = 0.5 + 0.5 * np.sin(self._field * 6.283 * 2.0 - t * 0.8)
            intensity = np.maximum(intensity, wave * self.cfg.render.background_level)

        rgb = self._colour_field(t) * intensity[..., None]
        if self.channels == 4:
            px = rgb_to_rgbw(rgb, self.cfg.output.white_mode, self.cfg.output.white_gain)
        else:
            px = rgb
        px = np.clip(px * self.brightness, 0.0, 1.0)
        out = self._lut[(px * 255.0).astype(np.uint8)]
        return out.reshape(self.h * self.w, self.channels)

    def blank(self) -> np.ndarray:
        return np.zeros((self.h * self.w, self.channels), dtype=np.uint8)
