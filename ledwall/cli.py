"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request

import numpy as np

from . import __version__, patterns, segmentation
from .config import Config, ConfigError
from .ddp import DDPSender
from .geometry import build_map, format_wled_bus_table
from .palettes import names as palette_names

DEFAULT_CONFIG_PATHS = [
    "ledwall.yaml",
    os.path.expanduser("~/.config/ledwall/ledwall.yaml"),
    "/etc/ledwall/ledwall.yaml",
]
BUNDLED_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "config", "wall-50x100.yaml")


def find_config(explicit: str | None) -> str:
    if explicit:
        return explicit
    for p in DEFAULT_CONFIG_PATHS:
        if os.path.exists(p):
            return p
    if os.path.exists(BUNDLED_CONFIG):
        return BUNDLED_CONFIG
    raise ConfigError(
        "no config found. Pass -c/--config, or run `ledwall init` to create one.\n"
        "Looked in: " + ", ".join(DEFAULT_CONFIG_PATHS)
    )


def load(args) -> Config:
    return Config.load(find_config(getattr(args, "config", None)))


def _wled_info(host: str, timeout: float = 2.0) -> dict:
    with urllib.request.urlopen(f"http://{host}/json/info", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---------------------------------------------------------------- commands
def cmd_init(args) -> int:
    dest = args.path
    if os.path.exists(dest) and not args.force:
        print(f"{dest} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    with open(BUNDLED_CONFIG, "r", encoding="utf-8") as src, open(dest, "w", encoding="utf-8") as out:
        out.write(src.read())
    print(f"wrote {dest}")
    print("Edit the controller hosts under `wiring.controllers`, then run: ledwall doctor")
    return 0


def cmd_buses(args) -> int:
    cfg = load(args)
    wall = build_map(cfg)
    print(format_wled_bus_table(wall))
    print()
    print("In each WLED device: Config -> LED Preferences -> add one bus per output,")
    print("set 'Start' and 'Count' to the values above, type SK6812/WS2814 (RGBW),")
    print("and make sure the device's total LED count matches its chain total.")
    return 0


def cmd_doctor(args) -> int:
    problems = 0
    print(f"ledwall {__version__}   python {sys.version.split()[0]}   {sys.platform}")
    try:
        import platform

        print(f"machine: {platform.machine()}")
        if platform.machine() not in ("aarch64", "arm64", "x86_64"):
            print("  ! 32-bit ARM detected: mediapipe has no wheel for this.")
            print("    Use 64-bit Raspberry Pi OS, or set source.backend: mog2")
    except Exception:
        pass

    print("\ndependencies:")
    for name, status in segmentation.probe().items():
        flag = "ok " if status.startswith("ok") else "-- "
        print(f"  {flag}{name:<16} {status}")

    print("\nconfig:")
    try:
        cfg = load(args)
        print(f"  ok  loaded {cfg.path}")
    except ConfigError as exc:
        print(f"  !!  {exc}")
        return 1
    try:
        wall = build_map(cfg)
        print(f"  ok  {cfg.grid.width}x{cfg.grid.height} = {wall.total_leds} LEDs, "
              f"{len(wall.controllers)} controller(s), mapping is one-to-one")
    except ValueError as exc:
        print(f"  !!  {exc}")
        return 1

    bytes_per_frame = wall.total_leds * (4 if cfg.output.rgbw else 3)
    mbps = bytes_per_frame * cfg.output.fps * 8 / 1e6
    print(f"  --  {bytes_per_frame} bytes/frame, {mbps:.1f} Mbit/s at {cfg.output.fps:g} fps")

    print("\ncontrollers:")
    for c in wall.controllers:
        try:
            info = _wled_info(c.host)
            reported = int(info.get("leds", {}).get("count", -1))
            ver = info.get("ver", "?")
            if reported == c.led_count:
                print(f"  ok  {c.name:<10} {c.host:<16} WLED {ver}, {reported} LEDs (matches)")
            else:
                problems += 1
                print(f"  !!  {c.name:<10} {c.host:<16} WLED {ver} reports {reported} LEDs, "
                      f"config expects {c.led_count}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            problems += 1
            print(f"  !!  {c.name:<10} {c.host:<16} unreachable ({exc})")

    if cfg.source.kind != "pattern":
        print("\ncamera:")
        try:
            from .camera import list_devices

            devices = list_devices()
            if devices:
                for d in devices:
                    print(f"  ok  {d}")
            else:
                problems += 1
                print("  !!  no working /dev/video* device found")
        except Exception as exc:  # noqa: BLE001
            problems += 1
            print(f"  !!  camera probe failed: {exc}")

    print("\n" + ("all checks passed" if not problems else f"{problems} problem(s) found"))
    return 0 if not problems else 1


def cmd_test(args) -> int:
    cfg = load(args)
    wall = build_map(cfg)
    from .render import gamma_lut, rgb_to_rgbw

    lut = gamma_lut(cfg.output.gamma)
    channels = 4 if cfg.output.rgbw else 3
    senders = [DDPSender(c.host, c.port, rgbw=cfg.output.rgbw) for c in wall.controllers]
    bright = args.brightness if args.brightness is not None else cfg.output.brightness
    print(f"sending '{args.pattern}' to {len(senders)} controller(s) at "
          f"{bright:.0%} brightness, ctrl-C to stop")
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("now", True))
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("now", True))
    period = 1.0 / cfg.output.fps
    start = time.monotonic()
    frames = 0
    try:
        while not stop["now"]:
            phase = (time.monotonic() - start) * args.speed
            img = patterns.make(args.pattern, cfg.grid.width, cfg.grid.height, phase=phase)
            linear = img.astype(np.float32) / 255.0
            px = rgb_to_rgbw(linear, cfg.output.white_mode, cfg.output.white_gain) if channels == 4 else linear
            px = np.clip(px * bright, 0, 1)
            frame = lut[(px * 255).astype(np.uint8)].reshape(-1, channels)
            for sender, c in zip(senders, wall.controllers):
                sender.send_frame(frame[c.take])
            frames += 1
            if args.seconds and time.monotonic() - start > args.seconds:
                break
            time.sleep(period)
    finally:
        blank = np.zeros((cfg.grid.width * cfg.grid.height, channels), dtype=np.uint8)
        for sender, c in zip(senders, wall.controllers):
            sender.send_frame(blank[c.take])
            sender.close()
    print(f"\nsent {frames} frames")
    return 0


def cmd_map(args) -> int:
    """Light one output (or one strip run) at a time to verify the wiring."""
    cfg = load(args)
    wall = build_map(cfg)
    channels = 4 if cfg.output.rgbw else 3
    senders = {c.name: DDPSender(c.host, c.port, rgbw=cfg.output.rgbw) for c in wall.controllers}
    level = int(255 * (args.brightness if args.brightness is not None else cfg.output.brightness))
    colours = [(level, 0, 0), (0, level, 0), (0, 0, level), (level, level, 0),
               (level, 0, level), (0, level, level), (level, level, level), (level, level // 2, 0)]

    # Each step is a label plus the exact payload for every controller, so a
    # step that lights one output leaves the other controllers explicitly dark.
    blank = {c.name: np.zeros((c.led_count, channels), dtype=np.uint8) for c in wall.controllers}
    steps: list[tuple[str, dict[str, np.ndarray]]] = []

    if args.by == "output":
        for c in wall.controllers:
            for o in c.outputs:
                payload = {k: v.copy() for k, v in blank.items()}
                col = colours[o.index % len(colours)]
                payload[c.name][o.led_start : o.led_start + o.led_count, :3] = col
                steps.append(
                    (
                        f"{c.name} output {o.index}: LEDs {o.led_start}"
                        f"..{o.led_start + o.led_count - 1}, "
                        f"strip runs {o.first_line}..{o.first_line + o.line_count - 1}",
                        payload,
                    )
                )
    else:
        n_lines = cfg.grid.height if cfg.wiring.orientation == "rows" else cfg.grid.width
        for line in range(n_lines):
            grid = np.zeros((cfg.grid.height, cfg.grid.width, channels), dtype=np.uint8)
            if cfg.wiring.orientation == "rows":
                grid[line, :, :3] = (level, level, level)
                grid[line, 0, :3] = (level, 0, 0)  # red pip marks the left edge
            else:
                grid[:, line, :3] = (level, level, level)
                grid[0, line, :3] = (level, 0, 0)
            flat = grid.reshape(-1, channels)
            steps.append((f"strip run {line}", {c.name: flat[c.take] for c in wall.controllers}))

    print(f"{len(steps)} step(s), {args.hold}s each. Ctrl-C to stop.\n")
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("now", True))
    try:
        for label, buf in steps:
            if stop["now"]:
                break
            print(f"  -> {label}")
            for name, payload in buf.items():
                senders[name].send_frame(payload)
            deadline = time.monotonic() + args.hold
            while time.monotonic() < deadline and not stop["now"]:
                time.sleep(0.05)
    finally:
        for c in wall.controllers:
            senders[c.name].send_frame(np.zeros((c.led_count, channels), dtype=np.uint8))
            senders[c.name].close()
    return 0


def cmd_bench(args) -> int:
    cfg = load(args)
    wall = build_map(cfg)
    from .render import Renderer

    r = Renderer(cfg)
    mask = np.zeros((cfg.grid.height, cfg.grid.width), dtype=np.float32)
    mask[10:40, 20:60] = 1.0

    n = 200
    t0 = time.perf_counter()
    for i in range(n):
        frame = r.render(mask, i / 30.0)
    render_ms = (time.perf_counter() - t0) / n * 1000

    t0 = time.perf_counter()
    for _ in range(n):
        for c in wall.controllers:
            _ = frame[c.take]
    map_ms = (time.perf_counter() - t0) / n * 1000

    print(f"grid {cfg.grid.width}x{cfg.grid.height} = {wall.total_leds} LEDs")
    print(f"  render          {render_ms:6.2f} ms/frame")
    print(f"  wiring gather   {map_ms:6.2f} ms/frame")
    print(f"  headroom at {cfg.output.fps:g} fps: {1000/cfg.output.fps:.1f} ms budget, "
          f"{render_ms + map_ms:.2f} ms used")

    if args.segment:
        try:
            seg = segmentation.create(cfg.source.backend, model_path=cfg.source.model_path,
                                      options=cfg.source.options)
            img = np.random.randint(0, 255, (cfg.source.infer_size, cfg.source.infer_size, 3), dtype=np.uint8)
            seg.infer(img)  # warm up
            t0 = time.perf_counter()
            for _ in range(20):
                seg.infer(img)
            ms = (time.perf_counter() - t0) / 20 * 1000
            print(f"  segmentation    {ms:6.2f} ms/frame ({seg.name}) -> {1000/ms:.1f} fps ceiling")
            seg.close()
        except Exception as exc:  # noqa: BLE001
            print(f"  segmentation    unavailable: {exc}")
    return 0


def cmd_run(args) -> int:
    cfg = load(args)
    if args.source:
        cfg.source.kind = args.source
    if args.brightness is not None:
        cfg.output.brightness = args.brightness
    from .pipeline import Pipeline

    pipe = Pipeline(cfg)
    web = None
    if cfg.web.enabled and not args.no_web:
        from .web import WebServer

        try:
            web = WebServer(pipe, cfg.web).start()
            print(f"web preview: http://{cfg.web.host}:{cfg.web.port}/")
        except OSError as exc:
            print(f"web preview unavailable: {exc}", file=sys.stderr)

    stop = {"now": False}

    def _sig(*_):
        stop["now"] = True
        pipe._stop.set()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    print(f"{cfg.grid.width}x{cfg.grid.height} = {pipe.wall.total_leds} LEDs across "
          f"{len(pipe.wall.controllers)} controller(s); source={cfg.source.kind}")
    pipe.start()
    print(f"segmentation backend: {pipe.source.name}")
    try:
        pipe.run()
    finally:
        pipe.stop()
        if web:
            web.stop()
    print("stopped, wall blanked")
    return 0


def cmd_cameras(args) -> int:
    from .camera import list_devices

    found = list_devices()
    if not found:
        print("no working camera found under /dev/video*")
        return 1
    for d in found:
        print(d)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ledwall", description="Camera-driven WLED matrix over DDP")
    p.add_argument("--version", action="version", version=f"ledwall {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_config(sp):
        sp.add_argument("-c", "--config", help="path to config YAML")
        return sp

    r = with_config(sub.add_parser("run", help="run the wall"))
    r.add_argument("--source", choices=["person", "motion", "pattern"], help="override source.kind")
    r.add_argument("--brightness", type=float, help="override output.brightness (0-1)")
    r.add_argument("--no-web", action="store_true", help="disable the web preview")
    r.set_defaults(func=cmd_run)

    d = with_config(sub.add_parser("doctor", help="check deps, config, controllers and camera"))
    d.set_defaults(func=cmd_doctor)

    b = with_config(sub.add_parser("buses", help="print the WLED LED-settings table"))
    b.set_defaults(func=cmd_buses)

    t = with_config(sub.add_parser("test", help="send a test pattern (no camera needed)"))
    t.add_argument("pattern", nargs="?", default="gradient", choices=patterns.PATTERNS)
    t.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = forever)")
    t.add_argument("--speed", type=float, default=60.0, help="animation speed for 'chase'")
    t.add_argument("--brightness", type=float, help="override brightness (0-1)")
    t.set_defaults(func=cmd_test)

    m = with_config(sub.add_parser("map", help="light outputs/runs one at a time to verify wiring"))
    m.add_argument("--by", choices=["output", "line"], default="output")
    m.add_argument("--hold", type=float, default=2.0, help="seconds per step")
    m.add_argument("--brightness", type=float, help="override brightness (0-1)")
    m.set_defaults(func=cmd_map)

    bn = with_config(sub.add_parser("bench", help="measure render/mapping (and optionally model) speed"))
    bn.add_argument("--segment", action="store_true", help="also benchmark the segmentation model")
    bn.set_defaults(func=cmd_bench)

    i = sub.add_parser("init", help="write a starter config")
    i.add_argument("path", nargs="?", default="ledwall.yaml")
    i.add_argument("--force", action="store_true")
    i.set_defaults(func=cmd_init)

    c = sub.add_parser("cameras", help="list working video devices")
    c.set_defaults(func=cmd_cameras)

    pl = sub.add_parser("palettes", help="list palette names")
    pl.set_defaults(func=lambda a: (print("\n".join(palette_names())), 0)[1])

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
