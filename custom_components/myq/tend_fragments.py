"""Bounded reconstruction of Tend's fragmented RTP datagrams."""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass, field

_MAX_DATAGRAM = 2048
_MAX_FRAGMENTS = 512
_MAX_PACKET = 1_048_576
_MAX_BUFFER = 2 * _MAX_PACKET
_MAX_GROUPS = 16
_REORDER_SECONDS = 0.5


@dataclass(slots=True)
class _Fragments:
    count: int
    since: float
    parts: dict[int, bytes] = field(default_factory=dict)
    size: int = 0


class FragmentAssembler:
    """Emit whole packets in sequence; a gap invalidates dependent video frames."""

    def __init__(self, on_loss: Callable[[], None]) -> None:
        self._on_loss = on_loss
        self.expected: int | None = None
        self.groups: dict[int, _Fragments] = {}
        self.buffered = 0

    @property
    def deadline(self) -> float | None:
        return min((g.since + _REORDER_SECONDS for g in self.groups.values()), default=None)

    def clear(self) -> None:
        self.expected = None
        self.groups.clear()
        self.buffered = 0

    def _discard(self, next_sequence: int | None = None) -> None:
        self.clear()
        self.expected = next_sequence
        self._on_loss()

    def expire(self, now: float) -> list[bytes]:
        deadline = self.deadline
        if deadline is None or now < deadline:
            return []
        self._on_loss()
        # Skip missing/incomplete packets, then preserve the next contiguous run.
        # A later gap is handled separately so an IDR before it cannot make
        # dependent frames after that gap safe to publish.
        while self.groups:
            expected = self.expected
            assert expected is not None
            nearest = min(self.groups, key=lambda seq: (seq - expected) & 0xFFFFFFFF)
            group = self.groups[nearest]
            self.expected = nearest
            if len(group.parts) == group.count:
                return self._drain()
            self.buffered -= group.size
            del self.groups[nearest]
            self.expected = (nearest + 1) & 0xFFFFFFFF
        return []

    def feed(self, data: bytes, now: float) -> list[bytes]:
        if not 9 <= len(data) <= _MAX_DATAGRAM:
            self._discard()
            return []
        sequence, count, index = struct.unpack_from("<IHH", data)
        if not 1 <= count <= _MAX_FRAGMENTS or index >= count:
            self._discard((sequence + 1) & 0xFFFFFFFF)
            return []
        if self.expected is None:
            self.expected = sequence
        distance = (sequence - self.expected) & 0xFFFFFFFF
        if distance >= 0x80000000:
            return []  # A duplicate/late packet already delivered or discarded.
        if distance >= _MAX_GROUPS:
            self._discard(sequence)
        group = self.groups.setdefault(sequence, _Fragments(count, now))
        part = data[8:]
        if group.count != count or (index in group.parts and group.parts[index] != part):
            self._discard((sequence + 1) & 0xFFFFFFFF)
            return []
        if index in group.parts:
            return []
        group.parts[index] = part
        group.size += len(part)
        self.buffered += len(part)
        if group.size > _MAX_PACKET or self.buffered > _MAX_BUFFER:
            self._discard((sequence + 1) & 0xFFFFFFFF)
            return []
        return self._drain()

    def _drain(self) -> list[bytes]:
        packets = []
        while self.expected is not None and (ready := self.groups.get(self.expected)):
            if len(ready.parts) != ready.count:
                break
            packets.append(b"".join(ready.parts[i] for i in range(ready.count)))
            self.buffered -= ready.size
            del self.groups[self.expected]
            self.expected = (self.expected + 1) & 0xFFFFFFFF
        return packets


def rtp_payload(packet: bytes) -> bytes:
    """Strip RTP CSRCs, extension and padding without reading outside the packet."""
    if len(packet) < 12 or packet[0] >> 6 != 2:
        return b""
    offset = 12 + 4 * (packet[0] & 15)
    if packet[0] & 16:
        if offset + 4 > len(packet):
            return b""
        offset += 4 + 4 * struct.unpack_from(">H", packet, offset + 2)[0]
    end = len(packet)
    if packet[0] & 32:
        count = packet[-1]
        if not count or count > end - offset:
            return b""
        end -= count
    return packet[offset:end] if offset < end else b""
