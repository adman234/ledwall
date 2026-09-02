"""Person segmentation, with three interchangeable backends.

Which one you get depends on what installs on the Pi:

* ``mediapipe`` - best quality. Needs 64-bit Raspberry Pi OS; the published
  wheels are ``manylinux_2_28_aarch64`` and there is no 32-bit armv7l build.
* ``tflite``    - the same MediaPipe selfie-segmenter model driven directly
  through LiteRT/tflite-runtime. Lighter to install, nearly the same output.
* ``mog2``      - no ML at all: OpenCV background subtraction. Works
  everywhere, needs a static camera, and segments *movement* rather than
  people. The always-available fallback.
"""

from __future__ import annotations

import os
import urllib.request

import numpy as np

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/image_segmenter/"
    "selfie_segmenter/float16/1/selfie_segmenter.tflite"
)
DEFAULT_MODEL_DIR = os.path.expanduser("~/.cache/ledwall")
DEFAULT_MODEL_PATH = os.path.join(DEFAULT_MODEL_DIR, "selfie_segmenter.tflite")


class SegmentationError(RuntimeError):
    pass


def ensure_model(path: str = "", *, download: bool = True) -> str:
    """Return a local path to the selfie-segmenter model, fetching it if needed."""
    path = path or DEFAULT_MODEL_PATH
    if os.path.exists(path):
        return path
    if not download:
        raise SegmentationError(f"segmentation model missing at {path}")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".part"
    try:
        urllib.request.urlretrieve(MODEL_URL, tmp)
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise SegmentationError(f"could not download model from {MODEL_URL}: {exc}") from exc
    return path


class Segmenter:
    """Interface: BGR frame in, float32 mask 0..1 out (any resolution)."""

    name = "base"

    def infer(self, bgr: np.ndarray) -> np.ndarray:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        pass


class MediaPipeSegmenter(Segmenter):
    name = "mediapipe"

    def __init__(self, model_path: str = "", mask_index: int | None = None) -> None:
        import mediapipe as mp  # noqa: F401

        self._mp = mp
        self._mask_index = mask_index
        self._legacy = None
        self._seg = None
        try:
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision

            model = ensure_model(model_path)
            opts = vision.ImageSegmenterOptions(
                base_options=mp_python.BaseOptions(model_asset_path=model),
                running_mode=vision.RunningMode.IMAGE,
                output_category_mask=False,
                output_confidence_masks=True,
            )
            self._seg = vision.ImageSegmenter.create_from_options(opts)
            self._api = "tasks"
        except Exception:
            # Older mediapipe releases only have the legacy solutions API.
            self._legacy = mp.solutions.selfie_segmentation.SelfieSegmentation(model_selection=1)
            self._api = "solutions"

    def infer(self, bgr: np.ndarray) -> np.ndarray:
        import cv2

        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        if self._legacy is not None:
            return np.asarray(self._legacy.process(rgb).segmentation_mask, dtype=np.float32)
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        result = self._seg.segment(image)
        masks = result.confidence_masks
        if not masks:
            raise SegmentationError("mediapipe returned no confidence mask")
        idx = self._mask_index
        if idx is None:
            # selfie_segmenter emits [background, person]; a single-mask model
            # emits foreground probability directly.
            idx = 1 if len(masks) > 1 else 0
        return np.asarray(masks[idx].numpy_view(), dtype=np.float32)

    def close(self) -> None:
        for obj in (self._seg, self._legacy):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass


class TFLiteSegmenter(Segmenter):
    """Runs selfie_segmenter.tflite directly, introspecting its tensor shapes."""

    name = "tflite"

    def __init__(self, model_path: str = "", num_threads: int = 4) -> None:
        interpreter_cls = None
        errors = []
        for mod, attr in (
            ("ai_edge_litert.interpreter", "Interpreter"),
            ("tflite_runtime.interpreter", "Interpreter"),
            ("tensorflow.lite", "Interpreter"),
        ):
            try:
                m = __import__(mod, fromlist=[attr])
                interpreter_cls = getattr(m, attr)
                break
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{mod}: {exc}")
        if interpreter_cls is None:
            raise SegmentationError("no TFLite runtime available -> " + "; ".join(errors))

        model = ensure_model(model_path)
        self._it = interpreter_cls(model_path=model, num_threads=num_threads)
        self._it.allocate_tensors()
        self._in = self._it.get_input_details()[0]
        self._out = self._it.get_output_details()[0]
        _, self.in_h, self.in_w, _ = self._in["shape"]
        self.out_channels = int(self._out["shape"][-1])

    def infer(self, bgr: np.ndarray) -> np.ndarray:
        import cv2

        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(rgb, (int(self.in_w), int(self.in_h)), interpolation=cv2.INTER_AREA)
        if self._in["dtype"] == np.uint8:
            tensor = resized[None].astype(np.uint8)
        else:
            tensor = (resized[None].astype(np.float32) / 255.0)
        self._it.set_tensor(self._in["index"], tensor)
        self._it.invoke()
        out = np.asarray(self._it.get_tensor(self._out["index"]))[0].astype(np.float32)
        if out.ndim == 2:
            return out
        if out.shape[-1] == 1:
            return out[..., 0]
        # Multi-class output: softmax across classes, take the person class.
        e = np.exp(out - out.max(axis=-1, keepdims=True))
        probs = e / e.sum(axis=-1, keepdims=True)
        return probs[..., 1] if probs.shape[-1] > 1 else probs[..., 0]


class MOG2Segmenter(Segmenter):
    """Background subtraction. No model, no ML deps - needs a fixed camera."""

    name = "mog2"

    def __init__(self, history: int = 400, var_threshold: float = 24.0, learning_rate: float = -1.0) -> None:
        import cv2

        self._cv2 = cv2
        self._bg = cv2.createBackgroundSubtractorMOG2(
            history=history, varThreshold=var_threshold, detectShadows=True
        )
        self._lr = learning_rate
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    def infer(self, bgr: np.ndarray) -> np.ndarray:
        cv2 = self._cv2
        fg = self._bg.apply(bgr, learningRate=self._lr)
        # MOG2 marks shadows as 127; keep only hard foreground.
        _, fg = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, self._kernel)
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, self._kernel, iterations=2)
        return fg.astype(np.float32) / 255.0

    def reset(self) -> None:
        self.__init__()


def create(backend: str = "auto", *, model_path: str = "", options: dict | None = None) -> Segmenter:
    """Build a segmenter, falling back through the list when ``auto``."""
    options = options or {}
    order = [backend] if backend != "auto" else ["mediapipe", "tflite", "mog2"]
    errors = []
    for name in order:
        try:
            if name == "mediapipe":
                return MediaPipeSegmenter(model_path, options.get("mask_index"))
            if name == "tflite":
                return TFLiteSegmenter(model_path, int(options.get("num_threads", 4)))
            if name == "mog2":
                return MOG2Segmenter(
                    history=int(options.get("history", 400)),
                    var_threshold=float(options.get("var_threshold", 24.0)),
                )
            raise SegmentationError(f"unknown backend '{name}'")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
    raise SegmentationError("no segmentation backend available -> " + "; ".join(errors))


def probe() -> dict[str, str]:
    """What's importable right now - used by `ledwall doctor`."""
    status = {}
    for name, mod in (("mediapipe", "mediapipe"), ("tflite", "ai_edge_litert"), ("tflite-runtime", "tflite_runtime")):
        try:
            m = __import__(mod)
            status[name] = f"ok ({getattr(m, '__version__', 'unknown version')})"
        except Exception as exc:  # noqa: BLE001
            status[name] = f"unavailable ({exc.__class__.__name__})"
    try:
        import cv2

        status["opencv"] = f"ok ({cv2.__version__})"
    except Exception as exc:  # noqa: BLE001
        status["opencv"] = f"unavailable ({exc})"
    return status
