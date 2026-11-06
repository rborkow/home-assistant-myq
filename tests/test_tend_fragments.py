"""Synthetic fragment bounds, malformed RTP, and sequence rollover."""

import struct

from custom_components.myq.tend_fragments import FragmentAssembler, rtp_payload


def test_fragment_32_bit_sequence_wrap_and_duplicates() -> None:
    losses: list[bool] = []
    assembler = FragmentAssembler(lambda: losses.append(True))
    for seq in (0xFFFFFFFF, 0, 1):
        packet = struct.pack("<IHH", seq, 1, 0) + b"rtp"
        assert assembler.feed(packet, 0) == [b"rtp"]
        assert assembler.feed(packet, 0) == []
    assert not losses and assembler.buffered == 0


def test_malformed_and_conflicting_fragments_clear_bounded_state() -> None:
    losses: list[bool] = []
    assembler = FragmentAssembler(lambda: losses.append(True))
    first = struct.pack("<IHH", 0, 2, 0) + b"part"
    assert assembler.feed(first, 0) == []
    assert assembler.feed(first + b"conflict", 0) == []
    assert assembler.buffered == 0 and not assembler.groups
    for data in (b"short", b"x" * 2049, struct.pack("<IHH", 1, 65535, 0) + b"x"):
        assert assembler.feed(data, 0) == []
        assert assembler.buffered == 0 and not assembler.groups
    assert len(losses) == 4


def test_missing_sequence_window_cannot_accumulate_unbounded_packets() -> None:
    losses: list[bool] = []
    assembler = FragmentAssembler(lambda: losses.append(True))
    for seq in range(100):
        assembler.feed(struct.pack("<IHH", seq, 512, 0) + b"x" * 2040, 0)
        assert len(assembler.groups) <= 16
        assert assembler.buffered <= 2_097_152
    assert losses
    assembler.expire(1)
    assert assembler.buffered == 0 and assembler.deadline is None


def test_global_buffer_limit_clears_all_fragments() -> None:
    losses: list[bool] = []
    assembler = FragmentAssembler(lambda: losses.append(True))
    for seq in range(3):
        for index in range(400):
            assembler.feed(struct.pack("<IHH", seq, 512, index) + b"x" * 2040, 0)
            assert assembler.buffered <= 2_097_152
    assert losses


def test_rtp_csrc_extension_padding_and_truncation() -> None:
    header = b"\xb1\x66" + bytes(10) + bytes(4)
    extension = struct.pack(">HH", 0xBEDE, 1) + b"abcd"
    packet = header + extension + b"payload" + bytes(3) + b"\x04"
    assert rtp_payload(packet) == b"payload"
    for invalid in (b"short", header, header + extension[:-1], packet[:-1] + b"\xff"):
        assert rtp_payload(invalid) == b""
