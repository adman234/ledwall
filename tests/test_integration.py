"""End-to-end: pipeline -> DDP -> (fake WLED) and the web API."""

import json
import socket
import threading
import time
import urllib.request

import numpy as np
import pytest

from ledwall.config import Config
from ledwall.geometry import build_map
from ledwall.pipeline import Pipeline


class FakeWLED(threading.Thread):
    """Reassembles DDP frames the way WLED does, so we can check the result."""

    def __init__(self, expected_leds, channels=4):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.3)
        self.port = self.sock.getsockname()[1]
        self.expected = expected_leds * channels
        self.channels = channels
        self.buffer = bytearray(self.expected)
        self.frames = []
        self.bad = []
        self._stopping = threading.Event()

    def run(self):
        while not self._stopping.is_set():
            try:
                pkt, _ = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            if len(pkt) < 10:
                self.bad.append("short header")
                continue
            flags, _seq, dtype, dest = pkt[0], pkt[1], pkt[2], pkt[3]
            offset = int.from_bytes(pkt[4:8], "big")
            length = int.from_bytes(pkt[8:10], "big")
            body = pkt[10:]
            if flags & 0xC0 != 0x40:
                self.bad.append("bad version")
                continue
            if dest != 1:
                self.bad.append(f"bad destination {dest}")
                continue
            if dtype != (0x1B if self.channels == 4 else 0x0B):
                self.bad.append(f"bad data type {dtype:#x}")
                continue
            if length != len(body):
                self.bad.append("declared length != payload")
                continue
            if offset + length > self.expected:
                self.bad.append("write past end of chain")
                continue
            self.buffer[offset : offset + length] = body
            if flags & 0x01:  # push -> WLED renders here
                self.frames.append(bytes(self.buffer))

    def stop(self):
        self._stopping.set()
        self.join(timeout=2)
        self.sock.close()


@pytest.fixture
def wall_cfg():
    """A 20x10 wall on two fake controllers."""
    a = FakeWLED(100)
    b = FakeWLED(100)
    a.start()
    b.start()
    cfg = Config.from_dict(
        {
            "grid": {"width": 20, "height": 10},
            "output": {"fps": 60, "brightness": 1.0, "gamma": 1.0, "rgbw": True},
            "source": {"kind": "pattern", "smoothing": 0.0},
            "render": {"palette": "white", "background": "off", "cycle_seconds": 1e9},
            "web": {"enabled": False},
            "wiring": {
                "controllers": [
                    {"name": "a", "host": "127.0.0.1", "port": a.port, "lines": [0, 5], "outputs": 2},
                    {"name": "b", "host": "127.0.0.1", "port": b.port, "lines": [5, 10], "outputs": 2},
                ]
            },
        }
    )
    yield cfg, a, b
    a.stop()
    b.stop()


def test_pipeline_delivers_complete_frames_to_both_controllers(wall_cfg):
    cfg, a, b = wall_cfg
    pipe = Pipeline(cfg)
    pipe.start()
    try:
        for i in range(30):
            pipe.step(time.monotonic())
            time.sleep(0.005)
    finally:
        pipe.stop()
    time.sleep(0.4)

    assert not a.bad, a.bad[:5]
    assert not b.bad, b.bad[:5]
    assert len(a.frames) >= 25 and len(b.frames) >= 25
    assert all(len(f) == 400 for f in a.frames)  # 100 LEDs * 4 channels


def test_rendered_blob_lands_on_the_right_physical_leds(wall_cfg):
    cfg, a, b = wall_cfg
    wall = build_map(cfg)
    pipe = Pipeline(cfg, wall=wall)
    # Drive a known mask directly instead of the animated pattern source.
    pipe._target = np.zeros((10, 20), dtype=np.float32)
    pipe._target[0, 0] = 1.0  # top-left grid pixel only
    pipe.step(time.monotonic())
    time.sleep(0.3)

    frame = a.frames[-1]
    lit = [i for i in range(100) if any(frame[i * 4 : i * 4 + 4])]
    # top-left grid pixel is LED 0 on controller a (row 0 starts at the left)
    assert lit == [0]
    assert not b.frames or not any(b.frames[-1])
    pipe.stop()


def test_blackout_on_stop(wall_cfg):
    cfg, a, b = wall_cfg
    pipe = Pipeline(cfg)
    pipe._target = np.ones((10, 20), dtype=np.float32)
    pipe.step(time.monotonic())
    time.sleep(0.2)
    assert any(a.frames[-1])
    pipe.stop()
    time.sleep(0.3)
    assert not any(a.frames[-1]), "wall should be blanked on shutdown"


def test_web_api_reports_state_and_accepts_controls(wall_cfg):
    from ledwall.web import WebServer

    cfg, a, b = wall_cfg
    cfg.web.enabled = True
    cfg.web.port = 0  # let the OS pick
    pipe = Pipeline(cfg)
    server = WebServer(pipe, cfg.web)
    cfg.web.port = server.server.server_address[1]
    server.start()
    try:
        pipe.step(time.monotonic())
        url = f"http://127.0.0.1:{cfg.web.port}"
        state = json.loads(urllib.request.urlopen(url + "/api/state", timeout=3).read())
        assert state["leds"] == 200
        assert state["grid"] == {"width": 20, "height": 10}
        assert set(state["controllers"]) == {"a", "b"}

        req = urllib.request.Request(
            url + "/api/control",
            data=json.dumps({"brightness": 0.5, "palette": "ember", "mode": "glow"}).encode(),
            method="POST",
        )
        assert json.loads(urllib.request.urlopen(req, timeout=3).read())["ok"] is True
        assert pipe.renderer.brightness == 0.5
        assert pipe.renderer.palette == "ember"
        assert pipe.cfg.render.mode == "glow"

        page = urllib.request.urlopen(url + "/", timeout=3).read().decode()
        assert "ledwall" in page and "ember" in page  # palette list injected
    finally:
        server.stop()
        pipe.stop()


def test_frame_bytes_match_expected_wall_size():
    cfg = Config.load("config/wall-50x100.yaml")
    wall = build_map(cfg)
    assert wall.total_leds == 5000
    per_frame = wall.total_leds * 4
    assert per_frame == 20000
    # 1440-byte payloads -> 2500 LEDs per controller is 7 packets each
    assert -(-2500 * 4 // 1440) == 7
