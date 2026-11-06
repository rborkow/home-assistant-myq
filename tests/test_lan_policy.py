"""LAN-only candidate selection and media-source isolation."""

import asyncio
import ipaddress
import struct
from unittest.mock import MagicMock, patch

import pytest

from custom_components.myq.tend_media import VideoTransport, _Channel
from custom_components.myq.tend_protocol import TendCamera, TendStreamError, VideoOffer, _sdnk

CAMERA_IP = "192.168.1.20"
BROKER = ("203.0.113.10", 30000)


def transport(lan_only: bool = True, address: str = CAMERA_IP) -> VideoTransport:
    receiver = VideoTransport(
        TendCamera("camera", "camera:mac", "0123456789abcdef", address),
        VideoOffer("broker.example", BROKER[1], "0123456789abcdef", "LOG-test"),
        lambda _: None,
        lan_only=lan_only,
    )
    receiver._relay_ip = BROKER[0]
    receiver.local_ip = "192.168.1.10"
    receiver.channels = [
        _Channel(MagicMock(spec=asyncio.DatagramTransport), 40000, False),
        _Channel(MagicMock(spec=asyncio.DatagramTransport), 40001, True),
    ]
    return receiver


def candidates(*peers: tuple[str, int]) -> bytes:
    data = bytearray(_sdnk(2, bytes(90)))
    for offset, (host, port) in zip(range(20, 108, 22), peers, strict=False):
        data[offset : offset + 4] = ipaddress.IPv4Address(host).packed
        struct.pack_into(">H", data, offset + 20, port)
    return bytes(data)


@pytest.mark.parametrize("address", ["", "8.8.8.8", "127.0.0.1", "169.254.1.1", "192.0.2.1", "::1"])
def test_lan_only_requires_authenticated_rfc1918_address(address: str) -> None:
    with pytest.raises(TendStreamError):
        transport(address=address)
    # Existing routing remains available unless LAN-only was explicitly selected.
    assert transport(False, address)._lan_only is False


def test_only_exact_camera_candidate_from_exact_broker_can_pin_peer() -> None:
    receiver = transport()
    packet = candidates(("198.51.100.1", 9000), ("192.168.1.30", 9001), (CAMERA_IP, 9002))
    for remote in [(BROKER[0], BROKER[1] + 1), ("203.0.113.11", BROKER[1]), (CAMERA_IP, 9002)]:
        receiver.on_datagram(False, packet, remote)
        assert receiver.channels[0].peer is None
    receiver.on_datagram(False, packet, BROKER)
    assert receiver.channels[0].peer == (CAMERA_IP, 9002)
    receiver.on_datagram(False, candidates((CAMERA_IP, 9999)), BROKER)
    assert receiver.channels[0].peer == (CAMERA_IP, 9002)


def test_lan_only_never_selects_public_or_other_private_candidate() -> None:
    receiver = transport()
    receiver.on_datagram(False, candidates(("198.51.100.1", 9000), ("192.168.1.30", 9001)), BROKER)
    assert receiver.channels[0].peer is None
    with patch.object(receiver, "_handle_media") as media:
        receiver.on_datagram(False, b"media", BROKER)
        receiver.on_datagram(False, b"media", ("198.51.100.1", 9000))
        media.assert_not_called()


def test_pin_applies_to_media_rtcp_and_handshake_packets() -> None:
    receiver = transport()
    for channel in receiver.channels:
        channel.peer = (CAMERA_IP, 9000 + int(channel.control))
    with (
        patch.object(receiver, "_handle_media") as media,
        patch.object(receiver, "_handle_rtcp") as rtcp,
    ):
        for control in [False, True]:
            for remote in [BROKER, (CAMERA_IP, 9999), ("192.168.1.30", 9000)]:
                receiver.on_datagram(control, b"media", remote)
                receiver.on_datagram(control, _sdnk(5, b"\1"), remote)
            assert not receiver.channels[int(control)].connected
        media.assert_not_called()
        rtcp.assert_not_called()
        receiver.on_datagram(False, b"media", (CAMERA_IP, 9000))
        receiver.on_datagram(True, b"rtcp", (CAMERA_IP, 9001))
        media.assert_called_once_with(b"media")
        rtcp.assert_called_once_with(b"rtcp")
