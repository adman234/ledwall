"""Map grid pixels onto physical LED indices.

A wall is built from "lines" - one line is a single run of strip.  With the
default ``orientation: rows`` a line is a grid row of ``width`` pixels.

Each controller owns a contiguous block of lines, split across its physical
outputs.  WLED concatenates its outputs (buses) into one logical chain, and
DDP addresses that chain, so within a controller the LED index simply runs
0..n-1 across outputs in order.

The result is, per controller, a "gather" array ``take`` of length n_leds
where ``take[i]`` is the flat grid index (y * width + x) feeding LED i.
Rendering a frame is then one numpy fancy-index per controller.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import Config, ControllerCfg


@dataclass
class OutputMap:
    """One physical data output on a controller (a WLED bus)."""

    index: int
    first_line: int
    line_count: int
    led_start: int  # index within the controller's logical chain
    led_count: int


@dataclass
class ControllerMap:
    name: str
    host: str
    port: int
    led_count: int
    outputs: list[OutputMap]
    take: np.ndarray  # int32[led_count], values are flat grid indices

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass
class WallMap:
    width: int
    height: int
    orientation: str
    controllers: list[ControllerMap]

    @property
    def total_leds(self) -> int:
        return sum(c.led_count for c in self.controllers)

    def controller_by_name(self, name: str) -> ControllerMap | None:
        for c in self.controllers:
            if c.name == name:
                return c
        return None

    def coverage(self) -> np.ndarray:
        """How many LEDs each grid pixel drives - should be all ones."""
        counts = np.zeros(self.width * self.height, dtype=np.int32)
        for c in self.controllers:
            np.add.at(counts, c.take, 1)
        return counts.reshape(self.height, self.width)


def _line_pixels(
    line: int,
    reverse: bool,
    *,
    orientation: str,
    width: int,
    height: int,
    flip_line: bool,
    flip_pos: bool,
) -> np.ndarray:
    """Flat grid indices for one strip run, in the order the data travels."""
    if orientation == "rows":
        y = (height - 1 - line) if flip_line else line
        pos = np.arange(width, dtype=np.int64)
        if flip_pos:
            pos = pos[::-1]
        if reverse:
            pos = pos[::-1]
        return (y * width + pos).astype(np.int32)

    x = (width - 1 - line) if flip_line else line
    pos = np.arange(height, dtype=np.int64)
    if flip_pos:
        pos = pos[::-1]
    if reverse:
        pos = pos[::-1]
    return (pos * width + x).astype(np.int32)


def _build_controller(cfg: ControllerCfg, wiring, width: int, height: int) -> ControllerMap:
    orientation = wiring.orientation
    # start_corner says where line 0 sits and which way the first run travels.
    flip_line = wiring.start_corner.startswith("bottom") if orientation == "rows" else wiring.start_corner.endswith("right")
    flip_pos = wiring.start_corner.endswith("right") if orientation == "rows" else wiring.start_corner.startswith("bottom")

    counts = cfg.output_line_counts()
    outputs: list[OutputMap] = []
    chunks: list[np.ndarray] = []
    led_cursor = 0
    line_cursor = cfg.line_start

    for out_idx, n_lines in enumerate(counts):
        out_chunks = []
        for k in range(n_lines):
            line = line_cursor + k
            # Serpentine alternates direction every run.  Scoped to an output,
            # each physical strip run starts at the same edge (how you'd
            # actually wire a fresh output); scoped to the wall it alternates
            # continuously from line 0.
            phase = k if wiring.serpentine_scope == "output" else line
            reverse = bool(wiring.serpentine and phase % 2 == 1)
            out_chunks.append(
                _line_pixels(
                    line,
                    reverse,
                    orientation=orientation,
                    width=width,
                    height=height,
                    flip_line=flip_line,
                    flip_pos=flip_pos,
                )
            )
        arr = np.concatenate(out_chunks) if out_chunks else np.zeros(0, dtype=np.int32)
        outputs.append(
            OutputMap(
                index=out_idx,
                first_line=line_cursor,
                line_count=n_lines,
                led_start=led_cursor,
                led_count=int(arr.size),
            )
        )
        chunks.append(arr)
        led_cursor += int(arr.size)
        line_cursor += n_lines

    take = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int32)
    return ControllerMap(
        name=cfg.name,
        host=cfg.host,
        port=cfg.port,
        led_count=int(take.size),
        outputs=outputs,
        take=take,
    )


def build_map(cfg: Config) -> WallMap:
    """Build the full grid -> LED mapping and sanity-check its coverage."""
    w, h = cfg.grid.width, cfg.grid.height
    controllers = [_build_controller(c, cfg.wiring, w, h) for c in cfg.wiring.controllers]
    wall = WallMap(width=w, height=h, orientation=cfg.wiring.orientation, controllers=controllers)

    cov = wall.coverage()
    if not np.all(cov == 1):
        missing = int(np.count_nonzero(cov == 0))
        doubled = int(np.count_nonzero(cov > 1))
        raise ValueError(
            f"wiring map is not one-to-one: {missing} grid pixel(s) unlit, "
            f"{doubled} driven more than once (this is a bug in the wiring config)"
        )
    return wall


def wled_bus_table(wall: WallMap) -> list[dict]:
    """Rows for the LED-settings table you type into each WLED device."""
    rows = []
    for c in wall.controllers:
        for o in c.outputs:
            rows.append(
                {
                    "controller": c.name,
                    "host": c.host,
                    "output": o.index,
                    "start": o.led_start,
                    "count": o.led_count,
                    "lines": f"{o.first_line}..{o.first_line + o.line_count - 1}",
                }
            )
    return rows


def format_wled_bus_table(wall: WallMap) -> str:
    rows = wled_bus_table(wall)
    lines = [
        "WLED LED-settings (one row per physical output):",
        "",
        f"  {'controller':<12} {'host':<16} {'bus':>3} {'start':>7} {'count':>6}  strip runs",
        f"  {'-' * 12} {'-' * 16} {'-' * 3} {'-' * 7} {'-' * 6}  {'-' * 12}",
    ]
    for r in rows:
        lines.append(
            f"  {r['controller']:<12} {r['host']:<16} {r['output']:>3} "
            f"{r['start']:>7} {r['count']:>6}  {r['lines']}"
        )
    lines.append("")
    for c in wall.controllers:
        lines.append(f"  {c.name}: {c.led_count} LEDs total on the DDP chain")
    return "\n".join(lines)
