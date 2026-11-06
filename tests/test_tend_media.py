"""Check RTP sequence tracking and receiver reports."""

import struct

import pytest

from custom_components.myq.tend_media import VideoTransport
from custom_components.myq.tend_protocol import TendCamera, VideoOffer


@pytest.fixture
def transport() -> VideoTransport:
    return VideoTransport(
        TendCamera("camera", "camera:mac", "0123456789abcdef", "192.168.1.20"),
        VideoOffer("relay.example", 30000, "0123456789abcdef", "LOG-test"),
        lambda _: None,
    )


def _rtp(sequence: int, ssrc: int = 123) -> bytes:
    return b"\x80\x60" + struct.pack(">HII", sequence, 1000, ssrc)


@pytest.mark.parametrize(
    ("sequences", "extended_max", "lost", "fraction"),
    [
        ((100, 101, 102), 102, 0, 0),
        ((100, 102, 101, 103), 103, 0, 0),
        ((100, 101, 101, 102), 102, -1, 0),
        ((65534, 65535, 0, 1), 65537, 0, 0),
        ((65534, 0, 65535, 1), 65537, 0, 0),
        ((65534, 65535, 0, 65535, 1), 65537, -1, 0),
        ((65534, 0, 1), 65537, 1, 64),
    ],
)
def test_receiver_report_tracks_reordered_and_duplicate_packets(
    transport: VideoTransport,
    sequences: tuple[int, ...],
    extended_max: int,
    lost: int,
    fraction: int,
) -> None:
    for sequence in sequences:
        transport._observe_rtp(_rtp(sequence))
    report = transport._receiver_report()
    assert report is not None
    assert struct.unpack_from(">I", report, 16)[0] == extended_max
    assert int.from_bytes(report[13:16], "big", signed=True) == lost
    assert report[12] == fraction


def test_receiver_report_recovers_after_late_packet_between_reports(
    transport: VideoTransport,
) -> None:
    for sequence in (65534, 0):
        transport._observe_rtp(_rtp(sequence))
    first = transport._receiver_report()
    assert first is not None
    assert int.from_bytes(first[13:16], "big", signed=True) == 1

    for sequence in (65535, 1):
        transport._observe_rtp(_rtp(sequence))
    second = transport._receiver_report()
    assert second is not None
    assert struct.unpack_from(">I", second, 16)[0] == 65537
    assert int.from_bytes(second[13:16], "big", signed=True) == 0
    assert second[12] == 0


def test_receiver_report_resets_for_new_source(transport: VideoTransport) -> None:
    for sequence in (65534, 0, 65535, 1):
        transport._observe_rtp(_rtp(sequence))
    assert transport._receiver_report() is not None

    for sequence in (50, 51):
        transport._observe_rtp(_rtp(sequence, ssrc=456))
    report = transport._receiver_report()
    assert report is not None
    assert struct.unpack_from(">I", report, 8)[0] == 456
    assert struct.unpack_from(">I", report, 16)[0] == 51
    assert int.from_bytes(report[13:16], "big", signed=True) == 0
    assert report[12] == 0


def test_receiver_report_counts_each_full_sequence_cycle_once(
    transport: VideoTransport,
) -> None:
    for sequence in range(65534, 131075):
        transport._observe_rtp(_rtp(sequence % 65536))
        if sequence in (65536, 131072):
            transport._observe_rtp(_rtp(65535))
    report = transport._receiver_report()
    assert report is not None
    assert struct.unpack_from(">I", report, 16)[0] == 131074
    assert int.from_bytes(report[13:16], "big", signed=True) == -2
    assert report[12] == 0


def _nal(kind: int, value: bytes = b"synthetic") -> bytes:
    return b"\0\0\0\1" + bytes((kind,)) + value


def _packet(sequence: int, record: bytes, ssrc: int = 123) -> bytes:
    body = bytes(4) + struct.pack("<I", len(record)) + record
    return _rtp(sequence, ssrc) + b"w1" + struct.pack("<I", len(body)) + body


def _fragment(group: int, packet: bytes, count: int = 2) -> list[bytes]:
    split = (len(packet) + count - 1) // count
    parts = [packet[i : i + split] for i in range(0, len(packet), split)]
    return [struct.pack("<IHH", group, len(parts), i) + p for i, p in enumerate(parts)]


async def test_reordered_fragments_and_packets_deliver_once_without_next_packet(
    transport: VideoTransport,
) -> None:
    frames: list[bytes] = []
    transport.on_frame = frames.append
    keyframe = _nal(7) + _nal(8) + _nal(5)
    first = _fragment(65535, _packet(65535, keyframe))
    second = _fragment(65536, _packet(0, _nal(1)))
    try:
        transport._handle_media(first[1])
        for part in second:
            transport._handle_media(part)
        transport._handle_media(first[1])
        assert frames == []
        transport._handle_media(first[0])
        assert frames == [keyframe, _nal(1)]
        for part in first + second:
            transport._handle_media(part)
        assert frames == [keyframe, _nal(1)]
        report = transport._receiver_report()
        assert report is not None
        assert struct.unpack_from(">I", report, 16)[0] == 65536
        assert int.from_bytes(report[13:16], "big", signed=True) == 0
        assert transport._assembler.buffered == 0
        assert transport._fragment_timer is None
    finally:
        transport.close()


@pytest.mark.parametrize("partial", [True, False])
async def test_missing_packet_or_fragment_withholds_dependent_frames_until_idr(
    transport: VideoTransport, partial: bool
) -> None:
    from unittest.mock import patch

    frames: list[bytes] = []
    transport.on_frame = frames.append
    keyframe = _nal(7) + _nal(8) + _nal(5)
    try:
        with patch("custom_components.myq.tend_media.time.monotonic", return_value=10):
            for part in _fragment(0, _packet(0, keyframe)):
                transport._handle_media(part)
            if partial:
                transport._handle_media(_fragment(1, _packet(1, _nal(1)))[0])
            for part in _fragment(2, _packet(2, _nal(1))):
                transport._handle_media(part)
        assert frames == [keyframe]
        with patch("custom_components.myq.tend_media.time.monotonic", return_value=10.5):
            assert transport._fragment_timer is not None
            transport._fragment_timer.cancel()
            transport._expire_fragments()
            for part in _fragment(3, _packet(3, _nal(1))):
                transport._handle_media(part)
            assert frames == [keyframe]
            for part in _fragment(4, _packet(4, _nal(5))):
                transport._handle_media(part)
        assert frames == [keyframe, keyframe]
        report = transport._receiver_report()
        assert report is not None
        assert int.from_bytes(report[13:16], "big", signed=True) == 1
        assert transport._assembler.buffered == 0
    finally:
        transport.close()


async def test_new_rtp_source_requires_its_own_parameter_sets(transport: VideoTransport) -> None:
    frames: list[bytes] = []
    transport.on_frame = frames.append
    first = _nal(7) + _nal(8) + _nal(5)
    for group, source, record in [(0, 123, first), (1, 456, _nal(5)), (2, 456, first)]:
        for part in _fragment(group, _packet(group, record, source)):
            transport._handle_media(part)
    assert frames == [first, first]
    transport.close()


async def test_incomplete_packet_expires_without_more_network_input(
    transport: VideoTransport,
) -> None:
    import asyncio
    from unittest.mock import patch

    with patch("custom_components.myq.tend_fragments._REORDER_SECONDS", 0.01):
        transport._handle_media(_fragment(0, _packet(0, _nal(5)))[0])
        assert bool(transport._fragment_timer)
        await asyncio.sleep(0.03)
        assert transport._fragment_timer is None
        assert transport._assembler.buffered == 0
    transport.close()


async def test_idr_before_second_gap_does_not_release_later_dependent_frames(
    transport: VideoTransport,
) -> None:
    from unittest.mock import patch

    frames: list[bytes] = []
    transport.on_frame = frames.append
    keyframe = _nal(7) + _nal(8) + _nal(5)
    try:
        with patch("custom_components.myq.tend_media.time.monotonic", return_value=10):
            transport._handle_media(_fragment(0, _packet(0, _nal(1)))[0])
            for part in _fragment(1, _packet(1, keyframe)):
                transport._handle_media(part)
            transport._handle_media(_fragment(2, _packet(2, _nal(1)))[0])
            for part in _fragment(3, _packet(3, _nal(1))):
                transport._handle_media(part)
        with patch("custom_components.myq.tend_media.time.monotonic", return_value=10.5):
            for _ in range(2):
                assert transport._fragment_timer is not None
                transport._fragment_timer.cancel()
                transport._expire_fragments()
        assert frames == [keyframe]
        assert transport._assembler.buffered == 0
    finally:
        transport.close()
