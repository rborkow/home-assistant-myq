"""Synthetic LAN packets: privacy boundaries, bounded loss handling and policy."""

import asyncio
import socket
import struct
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.myq.keypad_lan_protocol import (
    MediaPacket,
    Reassembler,
    ReceiverReports,
    command,
    command_type,
    decrypt_media,
    discovery,
    identity,
    peer_candidate,
    private_ipv4,
)
from custom_components.myq.keypad_lan_session import LanPeer, _Channel, device_key
from custom_components.myq.keypad_transport import open_transport
from custom_components.myq.models import MyQCamera
from custom_components.myq.tend_protocol import TendStreamError

KEY = b"synthetic-key123"
CAMERA = MyQCamera("VK-0001-12345678", True, "vkp-camera", None)


def media(unit: bytes = b"\0\0\0\1\x65synthetic") -> bytes:
    clear = struct.pack("<I", len(unit)) + unit
    padder = padding.PKCS7(128).padder()
    padded = padder.update(clear) + padder.finalize()
    iv = bytes(range(16))
    encryptor = Cipher(algorithms.AES(KEY), modes.CBC(iv)).encryptor()
    body = bytes((0x23, 0, 20, 1)) + iv + encryptor.update(padded) + encryptor.finalize()
    return b"w1" + struct.pack("<I", len(body)) + body


def fragments(sequence: int, payload: bytes, count: int = 2) -> list[bytes]:
    rtp = struct.pack(">BBHII", 0x80, 102, sequence & 0xFFFF, sequence * 4500, 123) + payload
    split = (len(rtp) + count - 1) // count
    pieces = [rtp[i : i + split] for i in range(0, len(rtp), split)]
    return [struct.pack("<IHH", sequence, len(pieces), i) + p for i, p in enumerate(pieces)]


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "0.0.0.0", "8.8.8.8", "169.254.1.1", "224.0.0.1", "::1", "192.0.0.1"]
)
def test_only_rfc1918_camera_candidates(address: str) -> None:
    with pytest.raises(ValueError):
        private_ipv4(address)
    assert private_ipv4("10.20.2.10") == "10.20.2.10"


def test_discovery_does_not_select_public_or_other_camera_candidate() -> None:
    data = bytearray(command(2, bytes(90)))
    data[64:68] = socket.inet_aton("10.20.2.10")
    struct.pack_into(">H", data, 84, 9488)
    assert peer_candidate(bytes(data), "10.20.2.10") == ("10.20.2.10", 9488)
    assert peer_candidate(bytes(data), "10.20.2.11") is None
    data[64:68] = socket.inet_aton("8.8.8.8")
    assert peer_candidate(bytes(data), "10.20.2.10") is None
    assert command_type(bytes(data) + b"extra") is None
    request = discovery("10.20.2.20", 10000, 0, identity("viewer") + identity("camera"))
    assert len(request) == 111 and command_type(request) == 1


def test_reordering_duplicates_and_exact_decryption() -> None:
    assembler = Reassembler()
    first = fragments(0, media())
    second = fragments(1, media())
    assert assembler.feed(second[1], 0) == []
    assert assembler.feed(first[1], 0.01) == []
    assert assembler.feed(first[1], 0.02) == []
    packets = assembler.feed(first[0], 0.03)
    assert [p.sequence for p in packets] == [0]
    packets += assembler.feed(second[0], 0.04)
    assert [p.sequence for p in packets] == [0, 1]
    assert decrypt_media(packets[0].payload, KEY) == [b"\0\0\0\1\x65synthetic"]
    assert assembler.feed(first[0], 0.05) == []
    assert assembler.buffered == 0 and not assembler.groups


def test_missing_fragment_detected_before_corrupt_gop_is_emitted() -> None:
    assembler = Reassembler()
    assert assembler.feed(fragments(0, media())[0], 0) == []
    for fragment in fragments(1, media()):
        assert assembler.feed(fragment, 0.1) == []
    with pytest.raises(TendStreamError, match="packet loss"):
        assembler.check_deadline(0.51)


def test_conflicting_fragment_and_unbounded_metadata_rejected() -> None:
    assembler = Reassembler()
    first = fragments(0, media())[0]
    assembler.feed(first, 0)
    with pytest.raises(ValueError, match="Conflicting"):
        assembler.feed(first[:-1] + bytes((first[-1] ^ 1,)), 0.01)
    for data in [b"short", struct.pack("<IHH", 0, 65535, 0) + b"x", b"x" * 2049]:
        with pytest.raises(ValueError):
            Reassembler().feed(data, 0)
    with pytest.raises(TendStreamError, match="buffer limit"):
        Reassembler().feed(fragments(100, media())[0], 0)


def test_invalid_encrypted_packet_is_never_published() -> None:
    packet = media()
    for invalid in [packet[:-1], b"w0" + packet[2:], packet[:6] + b"\x03" + packet[7:]]:
        with pytest.raises(ValueError):
            decrypt_media(invalid, KEY)
    with pytest.raises(ValueError):
        decrypt_media(packet, b"wrong-key-1234567")


def test_session_key_is_selected_for_exact_camera() -> None:
    properties = {
        "device-info-list": (
            "other:aa,AES=wrong-key-123456+RTP=1\nselected:bb,AES=synthetic-key123+RTP=1"
        )
    }
    assert device_key(properties, "selected:bb") == KEY
    with pytest.raises(ValueError):
        device_key(properties, "missing:cc")


def test_peer_requires_exact_source_ip_and_port_before_any_media_parse() -> None:
    peer = object.__new__(LanPeer)
    peer.server = ("203.0.113.1", 5338)
    peer.expected = "10.20.2.10"
    sock = MagicMock()
    peer.channels = {sock: _Channel(0, b"", (peer.expected, 9488), True)}
    for source in [(peer.expected, 9999), ("10.20.2.11", 9488), peer.server]:
        sock.recvfrom.return_value = (b"malformed", source)
        assert peer.receive(cast(socket.socket, sock), KEY, 0) == []
    sock.sendto.assert_not_called()


def test_rtcp_reports_include_matching_sender_timing_without_identity() -> None:
    report = ReceiverReports()
    report.received(MediaPacket(42, 90000, 123, b""), 1)
    sr = struct.pack(">BBHIIIIII", 0x80, 200, 6, 123, 0x12345678, 0x87654321, 90000, 1, 100)
    report.sender_report(sr, 2)
    output = report.packet(3)
    assert output[:4] == bytes((0x81, 201, 0, 7))
    assert struct.unpack_from(">I", output, 16)[0] == 42
    assert struct.unpack_from(">II", output, 24) == (0x56788765, 65536)
    assert output[32:34] == bytes((0x81, 202))


@pytest.mark.parametrize("policy,fallback", [("lan_only", False), ("lan_preferred", True)])
async def test_cloud_fallback_requires_explicit_policy(policy: str, fallback: bool) -> None:
    with (
        patch(
            "custom_components.myq.keypad_transport.KeypadLanSession.open", new_callable=AsyncMock
        ) as lan,
        patch(
            "custom_components.myq.keypad_transport.KeypadSession.open", new_callable=AsyncMock
        ) as relay,
    ):
        lan.side_effect = TendStreamError("LAN unavailable")
        callback = MagicMock()
        kwargs = dict(
            on_power=MagicMock(),
            on_phase=MagicMock(),
            on_finished=MagicMock(),
            on_fallback=callback,
        )
        if fallback:
            await open_transport(policy, CAMERA, "synthetic", MagicMock(), **kwargs)
            relay.assert_awaited_once()
            callback.assert_called_once()
        else:
            with pytest.raises(TendStreamError):
                await open_transport(policy, CAMERA, "synthetic", MagicMock(), **kwargs)
            relay.assert_not_awaited()
            callback.assert_not_called()


async def test_cancellation_never_triggers_cloud_fallback() -> None:
    with (
        patch(
            "custom_components.myq.keypad_transport.KeypadLanSession.open", new_callable=AsyncMock
        ) as lan,
        patch(
            "custom_components.myq.keypad_transport.KeypadSession.open", new_callable=AsyncMock
        ) as relay,
    ):
        lan.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await open_transport(
                "lan_preferred",
                CAMERA,
                "synthetic",
                MagicMock(),
                on_power=MagicMock(),
                on_phase=MagicMock(),
                on_finished=MagicMock(),
                on_fallback=MagicMock(),
            )
        relay.assert_not_awaited()


def test_lan_socket_setup_failure_closes_partial_resources() -> None:
    route = MagicMock()
    route.__enter__.return_value = route
    route.getsockname.return_value = ("192.168.1.10", 12345)
    first, second = MagicMock(), MagicMock()
    first.getsockname.return_value = ("192.168.1.10", 23456)
    second.bind.side_effect = OSError("bind failed")
    with (
        patch(
            "custom_components.myq.keypad_lan_session.socket.socket",
            side_effect=[route, first, second],
        ),
        pytest.raises(OSError),
    ):
        LanPeer(("203.0.113.1", 5338), "192.168.1.20", "viewer", "camera")
    first.close.assert_called_once()
    second.close.assert_called_once()


async def test_invalid_policy_cannot_open_any_transport() -> None:
    with (
        patch(
            "custom_components.myq.keypad_transport.KeypadLanSession.open", new_callable=AsyncMock
        ) as lan,
        patch(
            "custom_components.myq.keypad_transport.KeypadSession.open", new_callable=AsyncMock
        ) as relay,
        pytest.raises(TendStreamError, match="Unknown"),
    ):
        await open_transport(
            "typo",
            CAMERA,
            "token",
            MagicMock(),
            on_power=MagicMock(),
            on_phase=MagicMock(),
            on_finished=MagicMock(),
            on_fallback=MagicMock(),
        )
    lan.assert_not_awaited()
    relay.assert_not_awaited()


def test_packet_loss_discards_damaged_gop_until_next_idr() -> None:
    peer = object.__new__(LanPeer)
    sock = MagicMock()
    peer.server = ("203.0.113.1", 5338)
    address = ("192.168.1.20", 9000)
    peer.channels = {sock: _Channel(0, b"", address, True)}
    peer.reporter = ReceiverReports()
    peer.reassembler = Reassembler()
    peer.waiting_keyframe = True

    def receive(data: bytes, now: float) -> list[bytes]:
        sock.recvfrom.return_value = data, address
        return peer.receive(sock, KEY, now)

    initial = b"\0\0\0\1\x65first-idr"
    frames = []
    for fragment in fragments(0, media(initial)):
        frames.extend(receive(fragment, 0))
    assert frames == [initial]
    assert receive(fragments(1, media())[0], 0.1) == []
    peer.check_deadline(0.7)
    assert peer.waiting_keyframe and peer.reassembler.buffered == 0
    for fragment in fragments(2, media(b"\0\0\0\1\x41dependent")):
        assert receive(fragment, 0.8) == []
    restored = b"\0\0\0\1\x65next-idr"
    frames = []
    for fragment in fragments(3, media(restored)):
        frames.extend(receive(fragment, 0.9))
    assert frames == [restored] and not bool(peer.waiting_keyframe)
    report = peer.reporter.packet(1)
    loss = struct.unpack_from(">I", report, 12)[0]
    assert loss & 0xFFFFFF == 1
    assert loss >> 24 == 64
