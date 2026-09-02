"""Configuration model for a ledwall install.

The whole wall is described by one YAML file.  Everything downstream
(geometry, DDP, render) is driven from these dataclasses, so a build with a
different pixel count or wiring order only needs a config change.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from typing import Any

import yaml

START_CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")
ORIENTATIONS = ("rows", "columns")
SERPENTINE_SCOPES = ("output", "wall")
WHITE_MODES = ("none", "min", "accurate")


class ConfigError(ValueError):
    """Raised with a human-readable message when a config file is unusable."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def _pop_known(d: dict, where: str, known: set[str]) -> None:
    extra = set(d) - known
    _require(not extra, f"{where}: unknown key(s) {sorted(extra)}; expected one of {sorted(known)}")


@dataclass
class Grid:
    width: int = 100
    height: int = 50
    # Physical shape of one pixel cell: horizontal pitch / vertical pitch.
    # 1.0 for a square grid. If rows are spaced further apart than the LEDs
    # along a strip (e.g. 60/m strips on 24.4 mm row centres) this is < 1 and
    # the camera crop compensates so people aren't stretched.
    pixel_aspect: float = 1.0

    @property
    def count(self) -> int:
        return self.width * self.height

    @property
    def display_aspect(self) -> float:
        """Physical width/height of the lit area."""
        return (self.width / self.height) * self.pixel_aspect

    @classmethod
    def parse(cls, d: dict) -> "Grid":
        _pop_known(d, "grid", {"width", "height", "pixel_aspect"})
        g = cls(
            width=int(d.get("width", 100)),
            height=int(d.get("height", 50)),
            pixel_aspect=float(d.get("pixel_aspect", 1.0)),
        )
        _require(g.width > 0 and g.height > 0, "grid: width and height must be positive")
        _require(g.pixel_aspect > 0, "grid.pixel_aspect must be > 0")
        return g


@dataclass
class OutputCfg:
    """Frame-rate and colour handling for what we push to the wall."""

    fps: float = 30.0
    brightness: float = 0.35
    gamma: float = 2.2
    rgbw: bool = True
    white_mode: str = "accurate"
    white_gain: float = 1.0

    @classmethod
    def parse(cls, d: dict) -> "OutputCfg":
        _pop_known(d, "output", {"fps", "brightness", "gamma", "rgbw", "white_mode", "white_gain"})
        o = cls(
            fps=float(d.get("fps", 30.0)),
            brightness=float(d.get("brightness", 0.35)),
            gamma=float(d.get("gamma", 2.2)),
            rgbw=bool(d.get("rgbw", True)),
            white_mode=str(d.get("white_mode", "accurate")),
            white_gain=float(d.get("white_gain", 1.0)),
        )
        _require(o.fps > 0, "output.fps must be > 0")
        _require(0.0 <= o.brightness <= 1.0, "output.brightness must be between 0 and 1")
        _require(o.gamma > 0, "output.gamma must be > 0")
        _require(o.white_mode in WHITE_MODES, f"output.white_mode must be one of {WHITE_MODES}")
        return o


@dataclass
class ControllerCfg:
    """One WLED device.  Owns a contiguous range of strip runs ("lines")."""

    name: str
    host: str
    port: int = 4048
    line_start: int = 0
    line_end: int = 0
    # Either an int (split the range evenly across N physical outputs) or an
    # explicit list of per-output line counts.
    outputs: Any = 1

    @classmethod
    def parse(cls, d: dict, index: int, n_lines: int) -> "ControllerCfg":
        _pop_known(d, "controller", {"name", "host", "port", "lines", "rows", "columns", "outputs"})
        _require("host" in d, f"controllers[{index}]: 'host' is required")
        rng = d.get("lines", d.get("rows", d.get("columns")))
        _require(rng is not None, f"controllers[{index}]: needs 'lines' (or 'rows'/'columns') as [start, end)")
        _require(
            isinstance(rng, (list, tuple)) and len(rng) == 2,
            f"controllers[{index}]: 'lines' must be [start, end) - end is exclusive",
        )
        start, end = int(rng[0]), int(rng[1])
        _require(0 <= start < end <= n_lines, f"controllers[{index}]: lines {rng} outside 0..{n_lines}")
        outputs = d.get("outputs", 1)
        if isinstance(outputs, int):
            _require(outputs >= 1, f"controllers[{index}]: outputs must be >= 1")
            _require(
                outputs <= (end - start),
                f"controllers[{index}]: {outputs} outputs but only {end - start} strip runs to split",
            )
        else:
            _require(
                isinstance(outputs, list) and all(isinstance(v, int) and v > 0 for v in outputs),
                f"controllers[{index}]: outputs must be an int or a list of positive ints",
            )
            _require(
                sum(outputs) == end - start,
                f"controllers[{index}]: outputs {outputs} sum to {sum(outputs)}, "
                f"but the controller owns {end - start} strip runs",
            )
        return cls(
            name=str(d.get("name", f"wled-{index}")),
            host=str(d["host"]),
            port=int(d.get("port", 4048)),
            line_start=start,
            line_end=end,
            outputs=outputs,
        )

    def output_line_counts(self) -> list[int]:
        """Per-output strip-run counts, splitting as evenly as possible."""
        if isinstance(self.outputs, list):
            return list(self.outputs)
        total = self.line_end - self.line_start
        n = self.outputs
        base, extra = divmod(total, n)
        # Put the remainder on the first outputs so counts stay non-increasing.
        return [base + (1 if i < extra else 0) for i in range(n)]


@dataclass
class WiringCfg:
    orientation: str = "rows"
    start_corner: str = "top-left"
    serpentine: bool = True
    serpentine_scope: str = "output"
    controllers: list[ControllerCfg] = field(default_factory=list)

    @classmethod
    def parse(cls, d: dict, grid: Grid) -> "WiringCfg":
        _pop_known(
            d,
            "wiring",
            {"orientation", "start_corner", "serpentine", "serpentine_scope", "controllers"},
        )
        orientation = str(d.get("orientation", "rows"))
        _require(orientation in ORIENTATIONS, f"wiring.orientation must be one of {ORIENTATIONS}")
        start_corner = str(d.get("start_corner", "top-left"))
        _require(start_corner in START_CORNERS, f"wiring.start_corner must be one of {START_CORNERS}")
        scope = str(d.get("serpentine_scope", "output"))
        _require(scope in SERPENTINE_SCOPES, f"wiring.serpentine_scope must be one of {SERPENTINE_SCOPES}")

        n_lines = grid.height if orientation == "rows" else grid.width
        raw = d.get("controllers", [])
        _require(isinstance(raw, list) and raw, "wiring.controllers must be a non-empty list")
        controllers = [ControllerCfg.parse(c, i, n_lines) for i, c in enumerate(raw)]

        covered: dict[int, str] = {}
        for c in controllers:
            for line in range(c.line_start, c.line_end):
                _require(
                    line not in covered,
                    f"wiring: strip run {line} is claimed by both '{covered.get(line)}' and '{c.name}'",
                )
                covered[line] = c.name
        missing = [i for i in range(n_lines) if i not in covered]
        _require(
            not missing,
            f"wiring: {len(missing)} strip run(s) unassigned (first few: {missing[:8]}); "
            f"controllers must cover all {n_lines} runs",
        )
        return cls(orientation, start_corner, bool(d.get("serpentine", True)), scope, controllers)


@dataclass
class CameraCfg:
    device: Any = 0
    width: int = 640
    height: int = 480
    fps: float = 30.0
    fourcc: str = "MJPG"
    backend: str = "auto"  # auto | v4l2 | picamera2 | any
    mirror: bool = True
    rotate: int = 0  # 0 | 90 | 180 | 270

    @classmethod
    def parse(cls, d: dict) -> "CameraCfg":
        _pop_known(d, "camera", {"device", "width", "height", "fps", "fourcc", "backend", "mirror", "rotate"})
        c = cls(
            device=d.get("device", 0),
            width=int(d.get("width", 640)),
            height=int(d.get("height", 480)),
            fps=float(d.get("fps", 30.0)),
            fourcc=str(d.get("fourcc", "MJPG")),
            backend=str(d.get("backend", "auto")),
            mirror=bool(d.get("mirror", True)),
            rotate=int(d.get("rotate", 0)),
        )
        _require(c.rotate in (0, 90, 180, 270), "camera.rotate must be 0, 90, 180 or 270")
        _require(c.backend in ("auto", "v4l2", "picamera2", "any"), "camera.backend must be auto|v4l2|picamera2|any")
        return c


@dataclass
class SourceCfg:
    kind: str = "person"
    backend: str = "auto"  # auto | mediapipe | tflite | mog2
    model_path: str = ""
    infer_fps: float = 12.0
    infer_size: int = 256
    threshold: float = 0.5
    smoothing: float = 0.5
    fit: str = "cover"  # cover | stretch | contain
    options: dict = field(default_factory=dict)

    @classmethod
    def parse(cls, d: dict) -> "SourceCfg":
        _pop_known(
            d,
            "source",
            {"kind", "backend", "model_path", "infer_fps", "infer_size", "threshold",
             "smoothing", "fit", "options"},
        )
        s = cls(
            kind=str(d.get("kind", "person")),
            backend=str(d.get("backend", "auto")),
            model_path=str(d.get("model_path", "")),
            infer_fps=float(d.get("infer_fps", 12.0)),
            infer_size=int(d.get("infer_size", 256)),
            threshold=float(d.get("threshold", 0.5)),
            smoothing=float(d.get("smoothing", 0.5)),
            fit=str(d.get("fit", "cover")),
            options=dict(d.get("options", {})),
        )
        _require(s.infer_fps > 0, "source.infer_fps must be > 0")
        _require(0.0 <= s.smoothing < 1.0, "source.smoothing must be in [0, 1)")
        _require(s.kind in ("person", "motion", "pattern"), "source.kind must be person|motion|pattern")
        _require(s.fit in ("cover", "stretch", "contain"), "source.fit must be cover|stretch|contain")
        return s


@dataclass
class RenderCfg:
    palette: str = "aurora"
    mode: str = "fill"  # fill | outline | glow
    cycle_seconds: float = 24.0
    trail: float = 0.0
    background: str = "off"  # off | dim | idle
    background_level: float = 0.04

    @classmethod
    def parse(cls, d: dict) -> "RenderCfg":
        _pop_known(
            d,
            "render",
            {"palette", "mode", "cycle_seconds", "trail", "background", "background_level"},
        )
        r = cls(
            palette=str(d.get("palette", "aurora")),
            mode=str(d.get("mode", "fill")),
            cycle_seconds=float(d.get("cycle_seconds", 24.0)),
            trail=float(d.get("trail", 0.0)),
            background=str(d.get("background", "off")),
            background_level=float(d.get("background_level", 0.04)),
        )
        _require(0.0 <= r.trail < 1.0, "render.trail must be in [0, 1)")
        _require(r.mode in ("fill", "outline", "glow"), "render.mode must be fill|outline|glow")
        _require(r.background in ("off", "dim", "idle"), "render.background must be off|dim|idle")
        return r


@dataclass
class WebCfg:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    preview_scale: int = 6
    jpeg_quality: int = 70

    @classmethod
    def parse(cls, d: dict) -> "WebCfg":
        _pop_known(d, "web", {"enabled", "host", "port", "preview_scale", "jpeg_quality"})
        return cls(
            enabled=bool(d.get("enabled", True)),
            host=str(d.get("host", "0.0.0.0")),
            port=int(d.get("port", 8080)),
            preview_scale=int(d.get("preview_scale", 6)),
            jpeg_quality=int(d.get("jpeg_quality", 70)),
        )


@dataclass
class Config:
    grid: Grid = field(default_factory=Grid)
    output: OutputCfg = field(default_factory=OutputCfg)
    wiring: WiringCfg = field(default_factory=WiringCfg)
    camera: CameraCfg = field(default_factory=CameraCfg)
    source: SourceCfg = field(default_factory=SourceCfg)
    render: RenderCfg = field(default_factory=RenderCfg)
    web: WebCfg = field(default_factory=WebCfg)
    path: str = ""

    @classmethod
    def from_dict(cls, raw: dict, path: str = "") -> "Config":
        _require(isinstance(raw, dict), "config root must be a mapping")
        d = copy.deepcopy(raw)
        _pop_known(d, "config", {"grid", "output", "wiring", "camera", "source", "render", "web"})
        grid = Grid.parse(d.get("grid", {}) or {})
        return cls(
            grid=grid,
            output=OutputCfg.parse(d.get("output", {}) or {}),
            wiring=WiringCfg.parse(d.get("wiring", {}) or {}, grid),
            camera=CameraCfg.parse(d.get("camera", {}) or {}),
            source=SourceCfg.parse(d.get("source", {}) or {}),
            render=RenderCfg.parse(d.get("render", {}) or {}),
            web=WebCfg.parse(d.get("web", {}) or {}),
            path=path,
        )

    @classmethod
    def load(cls, path: str) -> "Config":
        path = os.path.expanduser(path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except FileNotFoundError:
            raise ConfigError(f"config file not found: {path}") from None
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path}: invalid YAML: {exc}") from None
        try:
            return cls.from_dict(raw, path=path)
        except ConfigError as exc:
            raise ConfigError(f"{path}: {exc}") from None
