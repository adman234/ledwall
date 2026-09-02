import socket
import threading

import numpy as np
import pytest

from ledwall.ddp import (
    DDP_FLAGS_PUSH,
    DDP_FLAGS_VER1,
    DDP_TYPE_RGB24,
    DDP_TYPE_RGBW32,
    DDPSender,
    build_packet,
)


def parse(pkt):
    return {
        "flags": pkt[0],
        "seq": pkt[1],
        "type": pkt[2],
        "dest": pkt[3],
        "offset": int.from_bytes(pkt[4:8], "big"),
        "length": int.from_bytes(pkt[8:10], "big"),
        "data": pkt[10:],
    }


def test_packet_header_matches_wled_expectations():
    p = parse(build_packet(pixel_offset=0, payload=b"\x01\x02\x03\x04", channels=4, sequence=3, push=True))
    assert p["flags"] == (DDP_FLAGS_VER1 | DDP_FLAGS_PUSH)
    assert p["type"] == DDP_TYPE_RGBW32
    assert p["dest"] == 1
    assert p["offset"] == 0
    assert p["length"] == 4
    # WLED requires the declared length to match the bytes actually present.
    assert len(p["data"]) == p["length"]


def test_rgb_type_and_channel_offset_is_in_bytes():
    p = parse(build_packet(pixel_offset=10, payload=b"\x00" * 3, channels=3, sequence=1, push=False))
    assert p["type"] == DDP_TYPE_RGB24
    assert p["offset"] == 30  # 10 pixels * 3 channels
    assert p["flags"] & DDP_FLAGS_PUSH == 0


class _Receiver:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.packets = []

    def collect(self, n, timeout=5.0):
        self.sock.settimeout(timeout)
        for _ in range(n):
            data, _ = self.sock.recvfrom(2048)
            self.packets.append(data)
        return self.packets


@pytest.fixture
def receiver():
    r = _Receiver()
    yield r
    r.sock.close()


def test_frame_is_split_and_reassembles_in_order(receiver):
    n_pixels = 2500
    pixels = np.arange(n_pixels * 4, dtype=np.uint8).reshape(n_pixels, 4)
    sender = DDPSender("127.0.0.1", receiver.port, rgbw=True)
    # 1440 bytes payload = 360 RGBW pixels -> ceil(2500/360) = 7 packets
    expected = 7
    t = threading.Thread(target=receiver.collect, args=(expected,))
    t.start()
    sender.send_frame(pixels)
    t.join(timeout=10)
    sender.close()

    assert len(receiver.packets) == expected
    parsed = [parse(p) for p in receiver.packets]
    # Only the final packet asks WLED to render.
    assert [bool(p["flags"] & DDP_FLAGS_PUSH) for p in parsed] == [False] * 6 + [True]
    # Offsets are contiguous and payloads reassemble to the original frame.
    offset = 0
    body = b""
    for p in parsed:
        assert p["offset"] == offset
        assert p["length"] == len(p["data"])
        assert p["length"] % 4 == 0  # never split a pixel across packets
        offset += p["length"]
        body += p["data"]
    assert body == pixels.tobytes()
    assert all(1 <= p["seq"] <= 15 for p in parsed)


def test_pixels_per_packet_is_whole_pixels():
    assert DDPSender("127.0.0.1", 1, rgbw=True).pixels_per_packet == 360
    assert DDPSender("127.0.0.1", 1, rgbw=False).pixels_per_packet == 480


def test_bad_frame_size_raises():
    s = DDPSender("127.0.0.1", 1, rgbw=True)
    with pytest.raises(ValueError, match="not a multiple"):
        s.send_frame(np.zeros(7, dtype=np.uint8))
    s.close()
