"""Mask sources: camera frame in, grid-sized 0..1 mask out."""

from __future__ import annotations

import math

import numpy as np

from . import segmentation


def fit_to_grid(
    img: np.ndarray, width: int, height: int, mode: str = "cover", display_aspect: float | None = None
) -> np.ndarray:
    """Resample a camera-shaped array onto the wall grid.

    ``display_aspect`` is the *physical* width/height of the lit area, which
    equals ``width/height`` only when the pixels are square. Passing it keeps
    people correctly proportioned on a wall whose row spacing differs from its
    along-strip spacing.

    ``cover`` centre-crops to that aspect before scaling (what you want for a
    person display), ``contain`` letter/pillarboxes, ``stretch`` distorts.
    """
    import cv2

    if mode == "stretch":
        return cv2.resize(img, (width, height), interpolation=cv2.INTER_AREA)

    ih, iw = img.shape[:2]
    target = display_aspect if display_aspect else width / height
    source = iw / ih

    if mode == "cover":
        if source > target:  # source too wide -> crop the sides
            new_w = max(int(round(ih * target)), 1)
            x0 = (iw - new_w) // 2
            img = img[:, x0 : x0 + new_w]
        else:  # source too tall -> crop top and bottom
            new_h = max(int(round(iw / target)), 1)
            y0 = (ih - new_h) // 2
            img = img[y0 : y0 + new_h, :]
        return cv2.resize(img, (width, height), interpolation=cv2.INTER_AREA)

    # contain: fit inside the wall in physical space, then squeeze the result
    # back into pixel rows (which may be non-square).
    phys_w = float(width)
    phys_h = phys_w / target
    scale = min(phys_w / iw, phys_h / ih)
    nw = min(max(int(round(iw * scale)), 1), width)
    nh = min(max(int(round(ih * scale * height / phys_h)), 1), height)
    small = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    out = np.zeros((height, width) + img.shape[2:], dtype=small.dtype)
    y0, x0 = (height - nh) // 2, (width - nw) // 2
    out[y0 : y0 + nh, x0 : x0 + nw] = small
    return out


def soft_threshold(mask: np.ndarray, threshold: float, softness: float = 0.15) -> np.ndarray:
    """Threshold with a soft edge - at 100x50 a hard cut looks jagged."""
    if softness <= 0:
        return (mask >= threshold).astype(np.float32)
    lo, hi = threshold - softness, threshold + softness
    return np.clip((mask - lo) / max(hi - lo, 1e-6), 0.0, 1.0).astype(np.float32)


class MaskSource:
    needs_camera = True
    name = "base"

    def compute(self, frame_bgr: np.ndarray | None, t: float) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:
        pass


class SegmentSource(MaskSource):
    """Person (or motion) segmentation downsampled onto the wall grid."""

    needs_camera = True

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.width = cfg.grid.width
        self.height = cfg.grid.height
        src = cfg.source
        backend = src.backend
        if src.kind == "motion":
            backend = "mog2"  # motion mode is background subtraction by definition
        self.segmenter = segmentation.create(
            backend, model_path=src.model_path, options=src.options
        )
        self.name = self.segmenter.name
        self.infer_size = src.infer_size
        self.threshold = src.threshold
        self.fit = src.fit
        self.display_aspect = cfg.grid.display_aspect

    def compute(self, frame_bgr: np.ndarray | None, t: float) -> np.ndarray:
        if frame_bgr is None:
            return np.zeros((self.height, self.width), dtype=np.float32)
        import cv2

        # Shrink before inference: the model runs at ~256px anyway and this is
        # the single biggest CPU saving on a Pi 3B+.
        h, w = frame_bgr.shape[:2]
        if self.infer_size and max(h, w) > self.infer_size:
            scale = self.infer_size / max(h, w)
            frame_bgr = cv2.resize(
                frame_bgr, (max(int(w * scale), 1), max(int(h * scale), 1)),
                interpolation=cv2.INTER_AREA,
            )
        raw = self.segmenter.infer(frame_bgr)
        mask = fit_to_grid(raw, self.width, self.height, self.fit, self.display_aspect)
        return soft_threshold(mask, self.threshold)

    def close(self) -> None:
        self.segmenter.close()


class PatternSource(MaskSource):
    """Animated stand-in so the wall can be exercised with no camera at all."""

    needs_camera = False
    name = "pattern"

    def __init__(self, cfg) -> None:
        self.width = cfg.grid.width
        self.height = cfg.grid.height
        ys, xs = np.mgrid[0 : self.height, 0 : self.width].astype(np.float32)
        self._xs = xs / max(self.width - 1, 1)
        self._ys = ys / max(self.height - 1, 1)

    def compute(self, frame_bgr: np.ndarray | None, t: float) -> np.ndarray:
        # A blob bouncing around, roughly person-shaped in scale.
        cx = 0.5 + 0.35 * math.sin(t * 0.7)
        cy = 0.5 + 0.30 * math.sin(t * 1.1 + 1.0)
        rx, ry = 0.10, 0.28
        d = ((self._xs - cx) / rx) ** 2 + ((self._ys - cy) / ry) ** 2
        return np.clip(1.5 - d, 0.0, 1.0).astype(np.float32)


def create(cfg) -> MaskSource:
    if cfg.source.kind == "pattern":
        return PatternSource(cfg)
    return SegmentSource(cfg)
