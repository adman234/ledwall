"""DDP (Distributed Display Protocol) output to WLED.

Wire format verified against WLED's receiver (``wled00/e131.cpp``
``handleDDPPacket``) and the constants in its bundled ESPAsyncE131 header:

    byte 0      flags: 0x40 = version 1, |0x01 = PUSH ("render now")
    byte 1      sequence number, low 4 bits, 0 means "unused"
    byte 2      data type: 0x0B = RGB 8bpc, 0x1B = RGBW 8bpc
    byte 3      destination id: 1 = display
    bytes 4-7   channel offset, uint32 big-endian, counted in CHANNELS (bytes)
    bytes 8-9   data length, uint16 big-endian, in bytes
    bytes 10+   pixel data

WLED renders when it sees the PUSH flag, so only the final packet of a frame
sets it.  It also rejects a packet whose declared length exceeds what actually
arrived, which is why payloads are capped at 1440 bytes (DDP's conventional
MTU-safe size, and divisible by both 3 and 4).
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field

import numpy as np

DDP_PORT = 4048
DDP_HEADER_LEN = 10
DDP_FLAGS_VER1 = 0x40
DDP_FLAGS_PUSH = 0x01
DDP_TYPE_RGB24 = 0x0B
DDP_TYPE_RGBW32 = 0x1B
DDP_ID_DISPLAY = 1
DDP_MAX_DATA = 1440


@dataclass
class SenderStats:
    frames: int = 0
    packets: int = 0
    bytes_sent: int = 0
    errors: int = 0
    last_error: str = ""


def build_packet(
    *,
    pixel_offset: int,
    payload: bytes,
    channels: int,
    sequence: int,
    push: bool,
) -> bytes:
    """Build one DDP packet.  Exposed separately so it can be unit-tested."""
    flags = DDP_FLAGS_VER1 | (DDP_FLAGS_PUSH if push else 0)
    dtype = DDP_TYPE_RGBW32 if channels == 4 else DDP_TYPE_RGB24
    offset = pixel_offset * channels
    header = bytes(
        (
            flags,
            sequence & 0x0F,
            dtype,
            DDP_ID_DISPLAY,
            (offset >> 24) & 0xFF,
            (offset >> 16) & 0xFF,
            (offset >> 8) & 0xFF,
            offset & 0xFF,
            (len(payload) >> 8) & 0xFF,
            len(payload) & 0xFF,
        )
    )
    return header + payload


class DDPSender:
    """UDP DDP output to a single WLED device."""

    def __init__(
        self,
        host: str,
        port: int = DDP_PORT,
        *,
        rgbw: bool = True,
        max_data: int = DDP_MAX_DATA,
        sndbuf: int = 1 << 20,
        sock: socket.socket | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.channels = 4 if rgbw else 3
        # Keep payloads a whole number of pixels so a pixel never straddles
        # two packets; WLED would still cope, but it makes offsets exact.
        self.max_data = (min(max_data, DDP_MAX_DATA) // self.channels) * self.channels
        self.stats = SenderStats()
        self._seq = 0
        self._owns_sock = sock is None
        self._sock = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self._owns_sock:
            try:
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
            except OSError:
                pass
            self._sock.setblocking(False)
            try:
                self._sock.connect((host, port))
                self._connected = True
            except OSError:
                self._connected = False
        else:
            self._connected = False
        # Reused per-packet buffer: header in place, payload copied after it.
        self._buf = bytearray(DDP_HEADER_LEN + self.max_data)
        self._view = memoryview(self._buf)

    @property
    def pixels_per_packet(self) -> int:
        return self.max_data // self.channels

    def _next_seq(self) -> int:
        self._seq = self._seq % 15 + 1  # 1..15; 0 means "sequence unused"
        return self._seq

    def send_frame(self, pixels: np.ndarray) -> int:
        """Send one frame. ``pixels`` is uint8, shape (n, channels) or flat."""
        arr = np.ascontiguousarray(pixels, dtype=np.uint8)
        data = arr.reshape(-1)
        total = data.size
        if total % self.channels:
            raise ValueError(
                f"frame has {total} bytes, not a multiple of {self.channels} channels/pixel"
            )
        raw = data.tobytes()
        sent = 0
        step = self.max_data
        for offset in range(0, total, step):
            chunk = raw[offset : offset + step]
            last = offset + step >= total
            flags = DDP_FLAGS_VER1 | (DDP_FLAGS_PUSH if last else 0)
            channel_offset = offset  # already in channels/bytes
            buf = self._buf
            buf[0] = flags
            buf[1] = self._next_seq()
            buf[2] = DDP_TYPE_RGBW32 if self.channels == 4 else DDP_TYPE_RGB24
            buf[3] = DDP_ID_DISPLAY
            buf[4] = (channel_offset >> 24) & 0xFF
            buf[5] = (channel_offset >> 16) & 0xFF
            buf[6] = (channel_offset >> 8) & 0xFF
            buf[7] = channel_offset & 0xFF
            buf[8] = (len(chunk) >> 8) & 0xFF
            buf[9] = len(chunk) & 0xFF
            buf[DDP_HEADER_LEN : DDP_HEADER_LEN + len(chunk)] = chunk
            packet = self._view[: DDP_HEADER_LEN + len(chunk)]
            try:
                if self._connected:
                    n = self._sock.send(packet)
                else:
                    n = self._sock.sendto(packet, (self.host, self.port))
                self.stats.packets += 1
                self.stats.bytes_sent += n
                sent += n
            except BlockingIOError:
                # Kernel send buffer full: drop this packet rather than stall
                # the render loop. The next frame supersedes it anyway.
                self.stats.errors += 1
                self.stats.last_error = "send buffer full"
            except OSError as exc:
                self.stats.errors += 1
                self.stats.last_error = str(exc)
        self.stats.frames += 1
        return sent

    def close(self) -> None:
        if self._owns_sock:
            try:
                self._sock.close()
            except OSError:
                pass


@dataclass
class WallSender:
    """Fans one grid frame out to every controller using the wiring map."""

    senders: list[DDPSender] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    takes: list[np.ndarray] = field(default_factory=list)

    @classmethod
    def from_map(cls, wall, *, rgbw: bool = True) -> "WallSender":
        senders, names, takes = [], [], []
        for c in wall.controllers:
            senders.append(DDPSender(c.host, c.port, rgbw=rgbw))
            names.append(c.name)
            takes.append(c.take)
        return cls(senders=senders, names=names, takes=takes)

    def send(self, frame_flat: np.ndarray) -> None:
        """``frame_flat`` is uint8 (width*height, channels) in grid order."""
        for sender, take in zip(self.senders, self.takes):
            sender.send_frame(frame_flat[take])

    def stats(self) -> dict[str, SenderStats]:
        return {n: s.stats for n, s in zip(self.names, self.senders)}

    def close(self) -> None:
        for s in self.senders:
            s.close()
