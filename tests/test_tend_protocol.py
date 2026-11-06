"""Protocol and local stream checks for TC cameras."""

from __future__ import annotations

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from custom_components.myq.tend_media import VideoTransport, _Channel
from custom_components.myq.tend_protocol import (
    CxsPacket,
    TendCamera,
    TendStreamError,
    VideoOffer,
    _sdnk,
    decode_video_payload,
    encode_cxs_packet,
    load_properties,
    parse_cameras,
    parse_peer_packet,
    register_packet,
    select_camera,
    store_properties,
)
from custom_components.myq.tend_signaling import CxsConnection

# Shape of Tend's login reply: "<account alias>:<camera MAC>,<caps>", GSN = attached opener.
DEVICE_INFO_LIST = (
    "myqalias:00:00:5E:00:53:01,AES=0123456789abcdef+GSN=door-2+LIP=192.168.1.20\n"
    "myqalias:00:00:5E:00:53:02,AES=abcdef0123456789+GSN=door-1+LIP=192.168.1.21"
)


def test_tend_cxs_and_udp_packets_match_protocol_layout() -> None:
    properties = {"CorrelationID": "LOG-test", "CX_DSTID": "myq:test", "multiline": "one\ntwo"}
    assert load_properties(store_properties(properties)) == properties

    packet = encode_cxs_packet(CxsPacket(519, b"body", source=b"source"))
    assert packet[:2] == b"A\x03"
    assert struct.unpack_from("<H", packet, 13)[0] == 519
    assert struct.unpack_from("<I", packet, 7)[0] == 4
    assert packet[27 + 16 : 27 + 16 + 6] == b"source"

    register = register_packet("myq123", "10.0.1.2", 20000, False)
    assert register[:4] == b"SDNK"
    assert register[7] == 1
    assert struct.unpack_from(">I", register, 16)[0] == len(register) - 20
    assert register[24] == ord("V")
    assert struct.unpack_from(">H", register, 45)[0] == 20000
    assert parse_peer_packet(bytes(109)) == []


def test_load_properties_follows_java_rules() -> None:
    text = (
        b"# a comment cannot continue \\\n"
        b"key1 = = value\n"
        b"key2==abc\n"
        b"key3:long \\\n"
        b"    value\n"
        b"key4 = ends with backslash \\\\\n"
        b"esc\\=aped=v\n"
        b"\fkey5\tv5\r\n"
        b"! another comment\n"
    )
    assert load_properties(text) == {
        "key1": "= value",
        "key2": "=abc",
        "key3": "long value",
        "key4": "ends with backslash \\",
        "esc=aped": "v",
        "key5": "v5",
    }


async def test_tend_video_payload_decrypts_h264_record() -> None:
    frame = b"\0\0\0\1\x67sps\0\0\0\1\x68pps\0\0\0\1\x65\x88\x84"
    combined = struct.pack("<I", len(frame)) + frame
    padder = PKCS7(128).padder()
    padded = padder.update(combined) + padder.finalize()
    iv = bytes(range(16))
    key = b"0123456789abcdef"
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encrypted = iv + encryptor.update(padded) + encryptor.finalize()
    body = b"\x20\x00\x00\x00" + encrypted
    payload = b"w1" + struct.pack("<I", len(body)) + body
    assert decode_video_payload(payload, key.decode()) == [frame]
    assert decode_video_payload(payload, "wrong") == []

    frames: list[bytes] = []
    transport = VideoTransport(
        TendCamera("12345", "12345:aa:bb", key.decode(), "192.168.1.20"),
        VideoOffer("192.168.1.30", 30000, key.decode(), "LOG-test"),
        frames.append,
    )
    rtp = b"\x80\x60" + struct.pack(">HII", 7, 1000, 123) + payload
    transport._handle_media(struct.pack("<IHH", 1, 2, 0) + rtp[:12])
    transport._handle_media(struct.pack("<IHH", 1, 2, 1) + rtp[12:])
    assert frames == [frame]
    report = transport._receiver_report()
    assert report is not None
    assert report[:2] == b"\x81\xc9"


def test_tend_camera_selection_data_and_nals() -> None:
    cameras = parse_cameras({"device-info-list": DEVICE_INFO_LIST})
    assert [camera.opener_serial for camera in cameras] == ["door-2", "door-1"]
    assert [camera.device_id for camera in cameras] == ["myqalias", "myqalias"]
    assert cameras[1].destination == "myqalias:00:00:5E:00:53:02"
    assert cameras[0].local_ip == "192.168.1.20"


async def test_signaling_reader_routes_replies_and_reports_loss() -> None:
    lost: list[CxsConnection] = []
    connection = CxsConnection(lost.append)
    reader = asyncio.StreamReader()
    reply_body = b"accept-video-receive|myqalias|9|"
    reply_packet = encode_cxs_packet(CxsPacket(411, reply_body, source=b"rtp://relay:5000"))
    connection._reader = reader
    # The camera answers the instant the command is sent.
    connection._writer = MagicMock(
        drain=AsyncMock(side_effect=lambda: reader.feed_data(reply_packet))
    )
    read_task = asyncio.create_task(connection._read_loop())
    connection._read_task = read_task

    reader.feed_data(encode_cxs_packet(CxsPacket(2000, b"device-info|unrequested")))
    correlation, reply = await asyncio.wait_for(
        connection.request(
            "require-video-send|myqalias|9|", "myqalias:mac", b"accept-video-receive|"
        ),
        2,
    )
    assert correlation.startswith("LOG-")
    assert reply.source == b"rtp://relay:5000"

    reader.feed_eof()
    await asyncio.wait_for(read_task, 2)
    assert lost == [connection]
    with pytest.raises(TendStreamError):
        await connection.command("stop-video-receive|myqalias|9|", "myqalias:mac")
    connection.close()


async def test_video_transport_ignores_datagrams_from_unknown_hosts() -> None:
    frames: list[bytes] = []
    transport = VideoTransport(
        TendCamera("myqalias", "myqalias:mac", "0123456789abcdef", "192.168.1.20"),
        VideoOffer("relay.example", 30000, "0123456789abcdef", "LOG-test"),
        frames.append,
    )
    transport._relay_ip = "203.0.113.5"
    transport.channels = [_Channel(MagicMock(), 20000, False), _Channel(MagicMock(), 20001, True)]
    media = struct.pack("<IHH", 1, 2, 0) + bytes(16)
    announced = bytearray(110)
    announced[:4] = b"SDNK"
    announced[7] = 2
    announced[20:24] = bytes((198, 51, 100, 7))
    struct.pack_into(">H", announced, 40, 9808)

    stranger = ("192.168.1.99", 9808)
    transport.on_datagram(False, bytes(announced), stranger)
    transport.on_datagram(False, _sdnk(5, b"\x01"), stranger)
    transport.on_datagram(False, media, stranger)
    transport.on_datagram(False, _sdnk(5, b"\x01"), ("198.51.100.7", 9808))
    assert transport.channels[0].peer is None
    assert transport._assembler.buffered == 0

    transport.on_datagram(False, bytes(announced), ("203.0.113.5", 30000))
    transport.on_datagram(False, _sdnk(5, b"\x01"), ("198.51.100.7", 9808))
    assert transport.channels[0].connected
    transport.on_datagram(False, media, ("192.168.1.20", 9808))
    assert transport._assembler.buffered == 16
    transport.close()


def test_malformed_payload_never_returns_a_valid_prefix() -> None:
    frame = b"\0\0\0\1\x65synthetic"
    prefix = struct.pack("<I", len(frame)) + frame
    for suffix in (b"x", struct.pack("<I", 100) + b"short", bytes(4)):
        body = bytes(4) + prefix + suffix
        payload = b"w1" + struct.pack("<I", len(body)) + body
        assert decode_video_payload(payload, "0123456789abcdef") == []


def test_select_camera_prefers_opener_then_unattached_keypad() -> None:
    listing = (
        DEVICE_INFO_LIST
        + "\nmyqalias:00:00:5E:00:53:03,AES=00+LIP=192.168.1.22+MODEL=VideoKeypadCam1"
        + "\nmyqalias:00:00:5E:00:53:04,AES=00+LIP=192.168.1.23+MODEL=OtherCam"
    )
    cameras = parse_cameras({"device-info-list": listing})

    attached = select_camera(cameras, "door-1")
    assert attached is not None and attached.destination.endswith("53:02")
    assert select_camera(cameras, "missing") is None
    keypad = select_camera(cameras, "")
    assert keypad is not None and keypad.model == "VideoKeypadCam1"


def test_select_camera_uses_only_an_unambiguous_unattached_camera() -> None:
    one = parse_cameras(
        {"device-info-list": DEVICE_INFO_LIST + "\nmyqalias:00:00:5E:00:53:03,LIP=192.168.1.22"}
    )
    selected = select_camera(one, "")
    assert selected is not None and selected.destination.endswith("53:03")
    two = parse_cameras(
        {
            "device-info-list": "a:00:00:5E:00:53:03,LIP=192.168.1.22\n"
            "a:00:00:5E:00:53:04,LIP=192.168.1.23"
        }
    )
    assert select_camera(two, "") is None
    assert select_camera(parse_cameras({"device-info-list": DEVICE_INFO_LIST}), "") is None
