"""Bounded Seedonk LAN framing, reordering, encrypted media and RTCP reports."""

from __future__ import annotations

import ipaddress
import secrets
import struct
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .tend_protocol import TendStreamError

MAX_PACKET = 1_048_576
MAX_BUFFER = 2 * MAX_PACKET
MAX_GROUPS = 16
REORDER_SECONDS = 0.5
_PRIVATE = tuple(
    ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class LanStreamError(TendStreamError):
    """A fixed, credential-free LAN failure suitable for diagnostics."""


class LanPacketLoss(LanStreamError):
    """A missing fragment/sequence; resume only at a subsequent keyframe."""


def private_ipv4(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Camera has no LAN address")
    address = ipaddress.IPv4Address(value)
    if not any(address in network for network in _PRIVATE):
        raise ValueError("Camera address is not RFC1918 LAN IPv4")
    return str(address)


def identity(alias: str) -> bytes:
    encoded = alias.encode("ascii")
    if not encoded or any(c in alias for c in "\r\n|\0"):
        raise ValueError("Invalid video identity")
    return (b"V_" + encoded[:30]).ljust(32, b"\0")


def command(kind: int, body: bytes, transaction: bytes = bytes(4)) -> bytes:
    if len(transaction) != 4 or len(body) > 128:
        raise ValueError("Invalid LAN command")
    return (
        b"SDNK"
        + bytes((1, 0, 0, kind))
        + transaction
        + bytes(4)
        + struct.pack(">I", len(body))
        + body
    )


def command_type(data: bytes) -> int | None:
    if (
        len(data) < 20
        or data[:7] != b"SDNK\x01\x00\x00"
        or struct.unpack_from(">I", data, 16)[0] != len(data) - 20
    ):
        return None
    return data[7]


def discovery(local: str, port: int, channel: int, identities: bytes) -> bytes:
    body = bytes((1, 0, 0, channel)) + b"V" + ipaddress.IPv4Address(local).packed
    body += bytes(16) + struct.pack(">H", port) + identities
    return command(1, body)


def peer_candidate(data: bytes, expected: str) -> tuple[str, int] | None:
    if command_type(data) != 2 or len(data) != 110:
        return None
    host = str(ipaddress.IPv4Address(data[64:68]))
    port = struct.unpack_from(">H", data, 84)[0]
    return (host, port) if host == expected and port else None


def decrypt_media(packet: bytes, key: bytes) -> list[bytes]:
    if not 42 <= len(packet) <= MAX_PACKET or packet[:2] != b"w1":
        raise ValueError("Invalid encrypted media header")
    declared = struct.unpack_from("<I", packet, 2)[0]
    if declared + 6 != len(packet) or packet[6] != 0x23 or len(key) != 16:
        raise ValueError("Invalid encrypted H.264 format")
    encrypted = packet[26:]
    if not encrypted or len(encrypted) % 16:
        raise ValueError("Invalid encrypted media length")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(packet[10:26])).decryptor()
    padded = decryptor.update(encrypted) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    clear = unpadder.update(padded) + unpadder.finalize()
    units: list[bytes] = []
    cursor = 0
    while cursor < len(clear):
        if cursor + 4 > len(clear) or len(units) >= 64:
            raise ValueError("Invalid access-unit framing")
        size = struct.unpack_from("<I", clear, cursor)[0]
        cursor += 4
        if size == 0 or cursor + size > len(clear):
            raise ValueError("Truncated access unit")
        unit = clear[cursor : cursor + size]
        if not unit.startswith((b"\0\0\1", b"\0\0\0\1")):
            raise ValueError("Invalid H.264 access unit")
        units.append(unit)
        cursor += size
    if not units:
        raise ValueError("Empty media packet")
    return units


@dataclass(slots=True)
class _Fragments:
    count: int
    since: float
    parts: dict[int, bytes] = field(default_factory=dict)
    size: int = 0


@dataclass(frozen=True, slots=True)
class MediaPacket:
    sequence: int
    timestamp: int
    ssrc: int
    payload: bytes


def parse_rtp(packet: bytes, sequence: int) -> MediaPacket:
    if len(packet) < 12 or packet[0] >> 6 != 2 or packet[1] & 0x7F != 102:
        raise ValueError("Invalid RTP video header")
    if struct.unpack_from(">H", packet, 2)[0] != sequence & 0xFFFF:
        raise ValueError("RTP/fragment sequence mismatch")
    offset = 12 + 4 * (packet[0] & 15)
    if packet[0] & 16:
        if offset + 4 > len(packet):
            raise ValueError("Truncated RTP extension")
        offset += 4 + 4 * struct.unpack_from(">H", packet, offset + 2)[0]
    end = len(packet)
    if packet[0] & 32:
        count = packet[-1]
        if not count or count > end - offset:
            raise ValueError("Invalid RTP padding")
        end -= count
    if offset >= end:
        raise ValueError("Truncated RTP payload")
    timestamp, ssrc = struct.unpack_from(">II", packet, 4)
    return MediaPacket(sequence, timestamp, ssrc, packet[offset:end])


class Reassembler:
    """Emit complete access units in order or end the session on an unrecoverable gap."""

    def __init__(self) -> None:
        self.expected: int | None = 0
        self.groups: dict[int, _Fragments] = {}
        self.buffered = 0
        self.ssrc: int | None = None

    def skip_gap(self) -> None:
        self.expected = None
        self.groups.clear()
        self.buffered = 0

    def check_deadline(self, now: float) -> None:
        if any(now - group.since > REORDER_SECONDS for group in self.groups.values()):
            raise LanPacketLoss("LAN video packet loss exceeded the reorder window")

    def feed(self, data: bytes, now: float) -> list[MediaPacket]:
        self.check_deadline(now)
        if not 9 <= len(data) <= 2048:
            raise ValueError("Invalid LAN fragment length")
        sequence, count, index = struct.unpack_from("<IHH", data)
        if not 1 <= count <= 512 or index >= count:
            raise ValueError("Invalid LAN fragment index")
        if self.expected is None:
            self.expected = sequence
        distance = (sequence - self.expected) & 0xFFFFFFFF
        if distance >= 0x80000000:
            return []  # Already delivered; UDP duplicates are harmless.
        if distance >= MAX_GROUPS:
            raise LanPacketLoss("LAN video sequence gap exceeds buffer limit")
        group = self.groups.setdefault(sequence, _Fragments(count, now))
        if group.count != count:
            raise ValueError("Conflicting LAN fragment count")
        part = data[8:]
        if index in group.parts:
            if group.parts[index] != part:
                raise ValueError("Conflicting duplicate LAN fragment")
            return []
        group.parts[index] = part
        group.size += len(part)
        self.buffered += len(part)
        if group.size > MAX_PACKET or self.buffered > MAX_BUFFER:
            raise LanStreamError("LAN video buffer limit reached")
        output: list[MediaPacket] = []
        while (ready := self.groups.get(self.expected)) and len(ready.parts) == ready.count:
            packet = parse_rtp(b"".join(ready.parts[i] for i in range(ready.count)), self.expected)
            if self.ssrc is not None and packet.ssrc != self.ssrc:
                raise ValueError("LAN video source changed")
            self.ssrc = packet.ssrc
            output.append(packet)
            self.buffered -= ready.size
            del self.groups[self.expected]
            self.expected = (self.expected + 1) & 0xFFFFFFFF
        return output


class ReceiverReports:
    """RFC 3550 receiver reports/SDES; no host or account identity in CNAME."""

    def __init__(self) -> None:
        self.local_ssrc = secrets.randbits(32)
        self.cname = secrets.token_hex(8).encode()
        self.source: int | None = None
        self.highest = 0
        self.first: int | None = None
        self.count = 0
        self.previous_expected = 0
        self.previous_count = 0
        self.jitter = 0.0
        self.transit: int | None = None
        self.last_sr = 0
        self.sr_at = 0.0

    def received(self, packet: MediaPacket, now: float) -> None:
        self.source = packet.ssrc
        self.highest = packet.sequence
        if self.first is None:
            self.first = packet.sequence
        self.count += 1
        transit = (int(now * 90000) - packet.timestamp) & 0xFFFFFFFF
        if self.transit is not None:
            delta = ((transit - self.transit + 0x80000000) & 0xFFFFFFFF) - 0x80000000
            self.jitter += (abs(delta) - self.jitter) / 16
        self.transit = transit

    def sender_report(self, data: bytes, now: float) -> None:
        cursor = 0
        while cursor + 4 <= len(data):
            if data[cursor] >> 6 != 2:
                return
            size = (struct.unpack_from(">H", data, cursor + 2)[0] + 1) * 4
            if size < 4 or cursor + size > len(data):
                return
            if data[cursor + 1] == 200 and size >= 28:
                source = struct.unpack_from(">I", data, cursor + 4)[0]
                if source == self.source:
                    self.last_sr = struct.unpack_from(">I", data, cursor + 10)[0]
                    self.sr_at = now
            cursor += size

    def packet(self, now: float) -> bytes:
        body = struct.pack(">I", self.local_ssrc)
        count = int(self.source is not None)
        if self.source is not None:
            delay = min(0xFFFFFFFF, int(max(0, now - self.sr_at) * 65536)) if self.last_sr else 0
            expected = ((self.highest - (self.first or 0)) & 0xFFFFFFFF) + 1
            interval = expected - self.previous_expected
            missing = interval - (self.count - self.previous_count)
            fraction = min(255, max(0, missing) * 256 // interval) if interval > 0 else 0
            lost = min(0x7FFFFF, max(0, expected - self.count))
            self.previous_expected, self.previous_count = expected, self.count
            body += struct.pack(
                ">IIIIII",
                self.source,
                (fraction << 24) | lost,
                self.highest,
                int(self.jitter),
                self.last_sr,
                delay,
            )
        rr = struct.pack(">BBH", 0x80 | count, 201, len(body) // 4) + body
        sdes = struct.pack(">I", self.local_ssrc) + bytes((1, len(self.cname))) + self.cname + b"\0"
        sdes += bytes((-len(sdes)) % 4)
        return rr + struct.pack(">BBH", 0x81, 202, len(sdes) // 4) + sdes
