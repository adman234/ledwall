"""Threaded camera capture.

The grab loop runs in its own thread and only ever keeps the newest frame.
On a Pi 3B+ that matters: if the render loop stalls, we drop stale frames
instead of playing back a growing backlog of them.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np


class CameraError(RuntimeError):
    pass


class Camera:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._frame: np.ndarray | None = None
        self._seq = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cap: Any = None
        self._picam: Any = None
        self.backend_name = ""
        self.frames_read = 0
        self.read_errors = 0
        self.last_error = ""

    # ---- opening -------------------------------------------------------
    def _open_picamera2(self):
        from picamera2 import Picamera2  # type: ignore

        cam = Picamera2()
        config = cam.create_video_configuration(
            main={"size": (self.cfg.width, self.cfg.height), "format": "RGB888"}
        )
        cam.configure(config)
        cam.start()
        self._picam = cam
        self.backend_name = "picamera2"

    def _open_v4l2(self, prefer_v4l2: bool):
        import cv2

        api = cv2.CAP_V4L2 if prefer_v4l2 else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.cfg.device, api)
        if not cap.isOpened():
            raise CameraError(f"could not open camera device {self.cfg.device!r}")
        fourcc = (self.cfg.fourcc or "").strip()
        if fourcc:
            # MJPG keeps USB bandwidth (and Pi CPU) far below raw YUYV.
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc[:4]))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.height)
        cap.set(cv2.CAP_PROP_FPS, self.cfg.fps)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        self._cap = cap
        self.backend_name = "v4l2" if prefer_v4l2 else "opencv"

    def open(self) -> None:
        backend = self.cfg.backend
        errors = []
        order = []
        if backend == "auto":
            order = ["v4l2", "picamera2", "any"]
        else:
            order = [backend]
        for b in order:
            try:
                if b == "picamera2":
                    self._open_picamera2()
                elif b == "v4l2":
                    self._open_v4l2(True)
                else:
                    self._open_v4l2(False)
                return
            except Exception as exc:  # noqa: BLE001 - report all attempts together
                errors.append(f"{b}: {exc}")
        raise CameraError("no camera backend worked -> " + "; ".join(errors))

    # ---- capture -------------------------------------------------------
    def _grab(self) -> np.ndarray | None:
        if self._picam is not None:
            return self._picam.capture_array()
        ok, frame = self._cap.read()
        return frame if ok else None

    def _postprocess(self, frame: np.ndarray) -> np.ndarray:
        import cv2

        if self.cfg.rotate:
            code = {
                90: cv2.ROTATE_90_CLOCKWISE,
                180: cv2.ROTATE_180,
                270: cv2.ROTATE_90_COUNTERCLOCKWISE,
            }[self.cfg.rotate]
            frame = cv2.rotate(frame, code)
        if self.cfg.mirror:
            frame = cv2.flip(frame, 1)
        return frame

    def _loop(self) -> None:
        misses = 0
        while not self._stop.is_set():
            try:
                frame = self._grab()
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                frame = None
            if frame is None:
                misses += 1
                self.read_errors += 1
                if misses >= 60:
                    # Camera fell off the USB bus; try to bring it back.
                    self.last_error = "camera stopped delivering frames, reopening"
                    self._reopen()
                    misses = 0
                time.sleep(0.02)
                continue
            misses = 0
            frame = self._postprocess(frame)
            with self._lock:
                self._frame = frame
                self._seq += 1
            self.frames_read += 1

    def _reopen(self) -> None:
        self.release_device()
        try:
            self.open()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"reopen failed: {exc}"
            time.sleep(1.0)

    def start(self) -> "Camera":
        if self._cap is None and self._picam is None:
            self.open()
        self._thread = threading.Thread(target=self._loop, name="camera", daemon=True)
        self._thread.start()
        return self

    def read(self) -> tuple[np.ndarray | None, int]:
        """Newest frame and its sequence number (None until the first arrives)."""
        with self._lock:
            if self._frame is None:
                return None, self._seq
            return self._frame, self._seq

    def wait_for_frame(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.read()[0] is not None:
                return True
            time.sleep(0.05)
        return False

    def release_device(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        if self._picam is not None:
            try:
                self._picam.stop()
                self._picam.close()
            except Exception:
                pass
            self._picam = None

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.release_device()


def list_devices(limit: int = 8) -> list[str]:
    """Probe /dev/video* for devices that actually deliver a frame."""
    import glob

    import cv2

    found = []
    for path in sorted(glob.glob("/dev/video*"))[:limit]:
        cap = cv2.VideoCapture(path, cv2.CAP_V4L2)
        if cap.isOpened():
            ok, frame = cap.read()
            if ok and frame is not None:
                found.append(f"{path} ({frame.shape[1]}x{frame.shape[0]})")
            else:
                found.append(f"{path} (opens, no frames)")
        cap.release()
    return found
