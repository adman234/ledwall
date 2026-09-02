"""The run loop.

Three rates, deliberately decoupled - this is what makes a Pi 3B+ feel smooth:

  capture thread    as fast as the webcam delivers, keeping only the newest frame
  inference thread  source.infer_fps (~10-12 on a Pi 3B+)
  output loop       output.fps (30), interpolating between masks

Segmentation is the expensive step, so it runs slower than the wall and the
output loop eases toward each new mask instead of stepping to it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np

from . import sources
from .camera import Camera
from .config import Config
from .ddp import WallSender
from .geometry import WallMap, build_map
from .render import Renderer


class _Rate:
    """Exponential moving average of events per second."""

    def __init__(self, alpha: float = 0.1) -> None:
        self.alpha = alpha
        self.value = 0.0
        self._last = 0.0

    def tick(self) -> None:
        now = time.monotonic()
        if self._last:
            dt = now - self._last
            if dt > 0:
                inst = 1.0 / dt
                self.value = inst if self.value == 0 else (1 - self.alpha) * self.value + self.alpha * inst
        self._last = now


@dataclass
class Stats:
    started: float = field(default_factory=time.monotonic)
    output_fps: float = 0.0
    infer_fps: float = 0.0
    camera_fps: float = 0.0
    frames_out: int = 0
    infer_count: int = 0
    infer_ms: float = 0.0
    coverage: float = 0.0
    camera_error: str = ""
    infer_error: str = ""

    @property
    def uptime(self) -> float:
        return time.monotonic() - self.started


class Pipeline:
    def __init__(self, cfg: Config, *, wall: WallMap | None = None, connect: bool = True) -> None:
        self.cfg = cfg
        self.wall = wall or build_map(cfg)
        self.renderer = Renderer(cfg)
        self.source = sources.create(cfg)
        self.stats = Stats()
        self.sender = WallSender.from_map(self.wall, rgbw=cfg.output.rgbw) if connect else None

        self.camera: Camera | None = Camera(cfg.camera) if self.source.needs_camera else None
        self._mask = np.zeros((cfg.grid.height, cfg.grid.width), dtype=np.float32)
        self._target = np.zeros_like(self._mask)
        self._mask_lock = threading.Lock()
        self._stop = threading.Event()
        self._infer_thread: threading.Thread | None = None
        self._out_rate = _Rate()
        self._infer_rate = _Rate()

        # Latest preview buffers for the web UI (grid RGB and camera BGR).
        self.preview_wall: np.ndarray | None = None
        self.preview_camera: np.ndarray | None = None
        self._preview_lock = threading.Lock()

    # ---- runtime controls (web UI / CLI) -------------------------------
    def set_brightness(self, value: float) -> None:
        self.renderer.brightness = max(0.0, min(1.0, float(value)))

    def set_palette(self, name: str) -> None:
        self.renderer.set_palette(name)

    def set_mode(self, mode: str) -> None:
        if mode in ("fill", "outline", "glow"):
            self.cfg.render.mode = mode

    # ---- threads --------------------------------------------------------
    def _infer_loop(self) -> None:
        period = 1.0 / max(self.cfg.source.infer_fps, 0.1)
        last_seq = -1
        while not self._stop.is_set():
            begin = time.monotonic()
            frame = None
            if self.camera is not None:
                frame, seq = self.camera.read()
                if seq == last_seq and frame is not None:
                    # No new camera frame yet; don't burn CPU re-segmenting it.
                    time.sleep(min(period, 0.01))
                    continue
                last_seq = seq
            try:
                t0 = time.monotonic()
                mask = self.source.compute(frame, time.monotonic() - self.stats.started)
                self.stats.infer_ms = (time.monotonic() - t0) * 1000.0
                with self._mask_lock:
                    self._target = mask
                self.stats.infer_count += 1
                self._infer_rate.tick()
                self.stats.infer_fps = self._infer_rate.value
                self.stats.infer_error = ""
            except Exception as exc:  # noqa: BLE001 - keep the wall alive
                self.stats.infer_error = f"{exc.__class__.__name__}: {exc}"
                time.sleep(0.25)
            if frame is not None:
                with self._preview_lock:
                    self.preview_camera = frame
            elapsed = time.monotonic() - begin
            if elapsed < period:
                self._stop.wait(period - elapsed)

    def start(self) -> None:
        if self.camera is not None:
            self.camera.start()
            if not self.camera.wait_for_frame(timeout=5.0):
                self.stats.camera_error = "no frames within 5s; continuing anyway"
        self._infer_thread = threading.Thread(target=self._infer_loop, name="infer", daemon=True)
        self._infer_thread.start()

    def step(self, now: float) -> np.ndarray:
        """Advance one output frame; returns the grid RGB(W) frame sent."""
        with self._mask_lock:
            target = self._target
        a = self.cfg.source.smoothing
        if a > 0:
            self._mask = self._mask * a + target * (1.0 - a)
        else:
            self._mask = target
        self.stats.coverage = float(self._mask.mean())
        frame = self.renderer.render(self._mask, now - self.stats.started)
        if self.sender is not None:
            self.sender.send(frame)
        self.stats.frames_out += 1
        self._out_rate.tick()
        self.stats.output_fps = self._out_rate.value
        if self.camera is not None:
            self.stats.camera_fps = 0.0 if self.camera.frames_read == 0 else self.stats.camera_fps
            self.stats.camera_error = self.camera.last_error
        return frame

    def run(self) -> None:
        period = 1.0 / self.cfg.output.fps
        next_frame = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            frame = self.step(now)
            self._store_wall_preview(frame)
            next_frame += period
            delay = next_frame - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                # Fell behind (a slow frame, or the loop was descheduled);
                # resync rather than accumulate an ever-growing deficit.
                next_frame = time.monotonic()

    def _store_wall_preview(self, frame: np.ndarray) -> None:
        h, w = self.cfg.grid.height, self.cfg.grid.width
        img = frame.reshape(h, w, -1)
        if img.shape[2] == 4:
            # Fold the white channel back in so the preview looks like the wall.
            rgb = np.clip(img[..., :3].astype(np.uint16) + img[..., 3:4].astype(np.uint16), 0, 255)
            img = rgb.astype(np.uint8)
        with self._preview_lock:
            self.preview_wall = img

    def previews(self) -> tuple[np.ndarray | None, np.ndarray | None]:
        with self._preview_lock:
            return self.preview_wall, self.preview_camera

    def blackout(self) -> None:
        """Push an all-off frame so the wall doesn't hold the last image."""
        if self.sender is not None:
            self.sender.send(self.renderer.blank())

    def stop(self) -> None:
        self._stop.set()
        if self._infer_thread is not None:
            self._infer_thread.join(timeout=2.0)
        if self.camera is not None:
            self.camera.stop()
        try:
            self.blackout()
        finally:
            if self.sender is not None:
                self.sender.close()
        self.source.close()

    def state(self) -> dict:
        senders = {}
        if self.sender is not None:
            for name, st in self.sender.stats().items():
                senders[name] = {
                    "frames": st.frames,
                    "packets": st.packets,
                    "bytes": st.bytes_sent,
                    "errors": st.errors,
                    "last_error": st.last_error,
                }
        return {
            "grid": {"width": self.cfg.grid.width, "height": self.cfg.grid.height},
            "leds": self.wall.total_leds,
            "source": self.source.name,
            "palette": self.renderer.palette,
            "mode": self.cfg.render.mode,
            "brightness": round(self.renderer.brightness, 3),
            "output_fps": round(self.stats.output_fps, 1),
            "infer_fps": round(self.stats.infer_fps, 1),
            "infer_ms": round(self.stats.infer_ms, 1),
            "frames_out": self.stats.frames_out,
            "coverage": round(self.stats.coverage, 3),
            "uptime": round(self.stats.uptime, 1),
            "camera": {
                "backend": self.camera.backend_name if self.camera else "none",
                "frames": self.camera.frames_read if self.camera else 0,
                "errors": self.camera.read_errors if self.camera else 0,
                "last_error": self.camera.last_error if self.camera else "",
            },
            "infer_error": self.stats.infer_error,
            "controllers": senders,
        }
