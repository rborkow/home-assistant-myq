"""Tend packet formats and camera descriptions.

Adapted from homebridge-myq-camera; see THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import ipaddress
import re
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

_CXS_HEADER = 27
_MAX_CXS_PACKET = 2 * 1024 * 1024
_SDNK_MAGIC = b"SDNK"
_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_PROPERTY_SPACE = " \t\f"


class TendStreamError(Exception):
    """A TC camera stream could not be opened or read."""


@dataclass(slots=True)
class CxsPacket:
    action: int
    body: bytes = b""
    options: bytes = bytes(16)
    source: bytes = b""
    result: int = 2
    destination: bytes = b"**"


def encode_cxs_packet(packet: CxsPacket) -> bytes:
    """Encode a CXS command or login packet."""
    if len(packet.options) % 16 or len(packet.destination) != 2:
        raise ValueError("Invalid CXS packet options")
    header_length = _CXS_HEADER + len(packet.options) + len(packet.source)
    if header_length > 65535 or len(packet.body) > _MAX_CXS_PACKET:
        raise ValueError("CXS packet is too large")
    header = bytearray(_CXS_HEADER)
    header[0:2] = b"A\x03"
    struct.pack_into(
        "<HHBIHHH",
        header,
        2,
        header_length,
        len(packet.options),
        0,
        len(packet.body),
        121,
        packet.action,
        packet.result,
    )
    header[17:19] = packet.destination
    return bytes(header) + packet.options + packet.source + packet.body


def _escape_property(value: str, *, key: bool = False) -> str:
    escaped = []
    for index, char in enumerate(value):
        if char == "\\":
            escaped.append("\\\\")
        elif char in "\t\n\r\f":
            escaped.append("\\" + {"\t": "t", "\n": "n", "\r": "r", "\f": "f"}[char])
        elif char in "=:" or (key and char in " #!") or (index == 0 and char == " "):
            escaped.append("\\" + char)
        elif ord(char) < 32 or ord(char) > 126:
            escaped.append(f"\\u{ord(char):04x}")
        else:
            escaped.append(char)
    return "".join(escaped)


def store_properties(values: dict[str, str]) -> bytes:
    """Encode the Java properties payload expected by the CXS broker."""
    lines = ["#myQ TC camera"]
    lines.extend(
        f"{_escape_property(k, key=True)}={_escape_property(v)}" for k, v in values.items()
    )
    return ("\n".join(lines) + "\n").encode("latin1")


def _unescape_property(value: str) -> str:
    return re.sub(
        r"\\(u[0-9a-fA-F]{4}|.)",
        lambda item: (
            chr(int(item[1][1:], 16))
            if item[1].startswith("u") and len(item[1]) == 5
            else {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(item[1], item[1])
        ),
        value,
    )


def load_properties(data: bytes) -> dict[str, str]:
    """Decode Java properties: comments, continuations, separators, and escapes."""
    result: dict[str, str] = {}
    lines = iter(_LINE_BREAK.split(data.decode("latin1")))
    for physical in lines:
        line = physical.lstrip(_PROPERTY_SPACE)
        if not line or line[0] in "#!":
            continue
        # An odd run of trailing backslashes continues the line; its leading space is dropped.
        while (len(line) - len(line.rstrip("\\"))) % 2:
            line = line[:-1] + next(lines, "").lstrip(_PROPERTY_SPACE)
        key_end = 0
        while key_end < len(line) and line[key_end] not in "=:" + _PROPERTY_SPACE:
            key_end += 2 if line[key_end] == "\\" else 1
        value = line[key_end:].lstrip(_PROPERTY_SPACE)
        if value[:1] in ("=", ":"):
            value = value[1:].lstrip(_PROPERTY_SPACE)
        result[_unescape_property(line[:key_end])] = _unescape_property(value)
    return result


@dataclass(slots=True)
class TendCamera:
    device_id: str
    destination: str
    aes_key: str
    local_ip: str
    opener_serial: str = ""
    model: str = ""


def parse_cameras(properties: dict[str, str]) -> list[TendCamera]:
    """Read the camera capabilities supplied by the Tend broker.

    Each destination is "<account alias>:<camera MAC>"; GSN names the attached opener.
    """
    cameras = []
    for line in properties.get("device-info-list", "").splitlines():
        destination, separator, capabilities = line.partition(",")
        if not separator or ":" not in destination:
            continue
        device_id = destination.split(":", 1)[0]
        raw = dict(item.split("=", 1) for item in capabilities.split("+") if "=" in item)
        cameras.append(
            TendCamera(
                device_id,
                destination,
                raw.get("AES", ""),
                raw.get("LIP", ""),
                raw.get("GSN", ""),
                raw.get("MODEL", ""),
            )
        )
    return cameras


def select_camera(cameras: list[TendCamera], opener_serial: str) -> TendCamera | None:
    """Pick the camera attached to an opener, or the account's one unattached camera.

    Unattached cameras (such as the Video Keypad) carry no opener serial, and the
    Tend listing has no field that maps back to their MyQ serial, so a Video Keypad
    model is preferred and otherwise only an unambiguous single candidate is used.
    """
    if opener_serial:
        return next((c for c in cameras if c.opener_serial == opener_serial), None)
    unattached = [c for c in cameras if not c.opener_serial]
    keypads = [c for c in unattached if c.model.casefold().startswith("videokeypadcam")]
    for group in (keypads, unattached):
        if len(group) == 1:
            return group[0]
    return None


@dataclass(slots=True)
class VideoOffer:
    relay_host: str
    relay_port: int
    aes_key: str
    correlation: str


def _sdnk(kind: int, payload: bytes) -> bytes:
    header = bytearray(20)
    header[:4] = _SDNK_MAGIC
    header[4:7] = b"\x01\x00\x00"
    header[7] = kind
    struct.pack_into(">I", header, 16, len(payload))
    return bytes(header) + payload


def _media_id(device_id: str) -> bytes:
    return ("V_" + device_id).encode("ascii")[:32].ljust(32, b"\x00")


def register_packet(device_id: str, local_ip: str, port: int, control: bool) -> bytes:
    """Register an RTP or RTCP UDP port with the Tend relay."""
    media_id = _media_id(device_id)
    payload = (
        bytes((1, 0, 0, int(control)))
        + b"V"
        + ipaddress.IPv4Address(local_ip).packed
        + bytes(16)
        + struct.pack(">H", port)
        + media_id
        + media_id
    )
    return _sdnk(1, payload)


def parse_peer_packet(data: bytes) -> list[tuple[str, int]]:
    """Read candidate camera IP and UDP ports from the relay response."""
    if len(data) != 110 or data[:4] != _SDNK_MAGIC or data[7] != 2:
        return []
    peers = []
    for offset in range(20, 108, 22):
        address = data[offset : offset + 4]
        port = struct.unpack_from(">H", data, offset + 20)[0]
        if any(address) and port:
            peers.append((str(ipaddress.IPv4Address(address)), port))
    return peers


def _decrypt(aes_key: str, blob: bytes) -> bytes:
    if len(aes_key) != 16 or len(blob) < 32:
        raise ValueError("Invalid Tend video encryption parameters")
    decryptor = Cipher(algorithms.AES(aes_key.encode("ascii")), modes.CBC(blob[:16])).decryptor()
    padded = decryptor.update(blob[16:]) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def decode_video_payload(payload: bytes, aes_key: str) -> list[bytes]:
    """Extract H.264 access units from a Tend RTP media payload."""
    if len(payload) < 10 or payload[:2] != b"w1":
        return []
    length = struct.unpack_from("<I", payload, 2)[0]
    if length + 6 != len(payload):
        return []
    combined = payload[10 : 10 + length - 4]
    if (payload[6] & 0xE0) >> 5 == 1:
        try:
            combined = _decrypt(aes_key, combined)
        except ValueError:
            return []
    records = []
    offset = 0
    while offset + 4 <= len(combined):
        record_length = struct.unpack_from("<I", combined, offset)[0]
        offset += 4
        if record_length == 0 or offset + record_length > len(combined):
            return []
        records.append(combined[offset : offset + record_length])
        offset += record_length
    return records if offset == len(combined) else []


def _nals(data: bytes) -> list[tuple[int, bytes]]:
    starts: list[tuple[int, int]] = []
    index = data.find(b"\x00\x00\x01")
    while index != -1:
        starts.append((index - 1, 4) if index and data[index - 1] == 0 else (index, 3))
        index = data.find(b"\x00\x00\x01", index + 3)
    units: list[tuple[int, bytes]] = []
    for position, (start, prefix) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(data)
        if end > start + prefix:
            units.append((data[start + prefix] & 0x1F, data[start:end]))
    return units
