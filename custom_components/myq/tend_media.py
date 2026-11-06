"""UDP transport, frame assembly and receiver reports for Tend video."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import secrets
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass

from .tend_fragments import FragmentAssembler, rtp_payload
from .tend_protocol import (
    _SDNK_MAGIC,
    TendCamera,
    TendStreamError,
    VideoOffer,
    _media_id,
    _nals,
    _sdnk,
    decode_video_payload,
    parse_peer_packet,
    register_packet,
)

_LOGGER = logging.getLogger(__name__)
_RTP_SEQUENCE_MODULUS = 1 << 16
_LAN_NETWORKS = tuple(
    ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def _camera_lan_ip(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as error:
        raise TendStreamError("Camera has no valid LAN IPv4 address") from error
    if not any(address in network for network in _LAN_NETWORKS):
        raise TendStreamError("Camera address is not RFC1918 LAN IPv4")
    return str(address)


@dataclass(slots=True)
class _Channel:
    transport: asyncio.DatagramTransport
    port: int
    control: bool
    peer: tuple[str, int] | None = None
    connected: bool = False


class _DatagramHandler(asyncio.DatagramProtocol):
    def __init__(self, owner: VideoTransport, control: bool) -> None:
        self.owner = owner
        self.control = control

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.owner.on_datagram(self.control, data, addr)

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("Tend video UDP error: %s", type(exc).__name__)


class VideoTransport:
    """LAN hole punching, RTP assembly, AES decoding, and RTCP reports."""

    def __init__(
        self,
        camera: TendCamera,
        offer: VideoOffer,
        on_frame: Callable[[bytes], None],
        *,
        lan_only: bool = False,
    ) -> None:
        self.camera = camera
        self._lan_only = lan_only
        self._lan_ip = _camera_lan_ip(camera.local_ip) if lan_only else ""
        self.offer = offer
        self.on_frame = on_frame
        self.channels: list[_Channel] = []
        self.local_ip = ""
        self._relay_ip = ""
        self._camera_ips = {camera.local_ip} if camera.local_ip else set[str]()
        self._connected = asyncio.Event()
        self._report_task: asyncio.Task[None] | None = None
        self._assembler = FragmentAssembler(self._invalidate_gop)
        self._fragment_timer: asyncio.TimerHandle | None = None
        self.media_discontinuities = 0
        self.gop_recoveries = 0
        self._recovering_gop = False
        self._waiting_idr = True
        self._sps: bytes | None = None
        self._pps: bytes | None = None
        self._local_ssrc = secrets.randbits(32)
        self._source_ssrc: int | None = None
        self._base_seq = 0
        self._max_seq = 0
        self._cycles = 0
        self._received = 0
        self._expected_prior = 0
        self._received_prior = 0
        self._last_transit: float | None = None
        self._jitter = 0.0
        self._last_sr = 0
        self._last_sr_at = 0.0

    async def _bind(self) -> None:
        loop = asyncio.get_running_loop()
        addresses = await loop.getaddrinfo(
            self.offer.relay_host,
            self.offer.relay_port,
            family=socket.AF_INET,
            type=socket.SOCK_DGRAM,
        )
        relay_ip = str(addresses[0][4][0])
        self._relay_ip = relay_ip
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.setblocking(False)
            await loop.sock_connect(probe, (self._lan_ip if self._lan_only else relay_ip, 9))
            self.local_ip = probe.getsockname()[0]
        finally:
            probe.close()
        if self._lan_only and self.local_ip == self._lan_ip:
            raise TendStreamError("Camera LAN address points to this host")
        bind_ip = self.local_ip if self._lan_only else "0.0.0.0"
        for _ in range(100):
            media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                media.bind((bind_ip, 0))
                media_port = media.getsockname()[1]
                if media_port == 65535:
                    media.close()
                    control.close()
                    continue
                control.bind((bind_ip, media_port + 1))
                for sock, is_control in ((media, False), (control, True)):

                    def protocol_factory(control_channel: bool = is_control) -> _DatagramHandler:
                        return _DatagramHandler(self, control_channel)

                    transport, _ = await loop.create_datagram_endpoint(protocol_factory, sock=sock)
                    self.channels.append(_Channel(transport, sock.getsockname()[1], is_control))
                return
            except OSError:
                for channel in self.channels:
                    channel.transport.close()
                self.channels.clear()
                media.close()
                control.close()
        raise TendStreamError("Could not allocate Tend video UDP ports")

    def _send(self, channel: _Channel, data: bytes, peer: tuple[str, int]) -> None:
        channel.transport.sendto(data, peer)

    async def punch(self, wait_seconds: float = 8) -> None:
        """Establish media and control UDP paths to the camera."""
        await self._bind()
        relay = (self._relay_ip, self.offer.relay_port)

        async def drive() -> None:
            sequence = 0
            while not self._connected.is_set():
                for channel in self.channels:
                    if channel.peer is None:
                        self._send(
                            channel,
                            register_packet(
                                self.camera.device_id, self.local_ip, channel.port, channel.control
                            ),
                            relay,
                        )
                    elif not channel.connected:
                        media_id = _media_id(self.camera.device_id)
                        self._send(
                            channel,
                            _sdnk(3, bytes((1, sequence)) + media_id + media_id),
                            channel.peer,
                        )
                        self._send(channel, _sdnk(4, b"\x01"), channel.peer)
                sequence = min(sequence + 1, 2)
                await asyncio.sleep(0.1)

        driver = asyncio.create_task(drive())
        try:
            await asyncio.wait_for(self._connected.wait(), wait_seconds)
        except TimeoutError as error:
            raise TendStreamError("Tend video UDP connection timed out") from error
        finally:
            driver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await driver
        self._report_task = asyncio.create_task(self._report_loop())

    def on_datagram(self, control: bool, data: bytes, remote: tuple[str, int]) -> None:
        """Handle a Seedonk setup packet or an RTP/RTCP datagram."""
        if len(self.channels) < 2:
            return
        channel = self.channels[int(control)]
        if self._lan_only:
            if data[:4] == _SDNK_MAGIC and len(data) >= 20 and data[7] == 2:
                if (
                    remote != (self._relay_ip, self.offer.relay_port)
                    or channel.peer is not None
                    or len(data) != 110
                    or data[:7] != _SDNK_MAGIC + b"\x01\x00\x00"
                    or struct.unpack_from(">I", data, 16)[0] != 90
                ):
                    return
                # Only the private identity supplied by authenticated TLS signaling is eligible.
                channel.peer = next(
                    (peer for peer in parse_peer_packet(data) if peer[0] == self._lan_ip), None
                )
                if channel.peer is not None:
                    media_id = _media_id(self.camera.device_id)
                    self._send(
                        channel,
                        _sdnk(100, bytes((1, int(control))) + b"V" + media_id + media_id),
                        remote,
                    )
                return
            # Broker packets never carry media; neither IP nor port may change after selection.
            if channel.peer is None or remote != channel.peer:
                return
        # Only the relay and the camera addresses it announced may set peers or send media.
        if remote[0] != self._relay_ip and remote[0] not in self._camera_ips:
            return
        if data[:4] == _SDNK_MAGIC and len(data) >= 20:
            kind = data[7]
            if kind == 2:
                if remote[0] != self._relay_ip:
                    return
                peers = parse_peer_packet(data)
                self._camera_ips.update(address for address, _ in peers)
                private = [
                    peer
                    for peer in peers
                    if ipaddress.ip_address(peer[0]).is_private and peer[0] != self.local_ip
                ]
                channel.peer = (
                    next((peer for peer in peers if peer[0] == self.camera.local_ip), None)
                    or (private[0] if private else None)
                    or (peers[-1] if peers else None)
                )
                if channel.peer is not None:
                    media_id = _media_id(self.camera.device_id)
                    self._send(
                        channel,
                        _sdnk(100, bytes((1, int(control))) + b"V" + media_id + media_id),
                        (self._relay_ip, self.offer.relay_port),
                    )
            elif kind == 3:
                channel.peer = remote
                media_id = _media_id(self.camera.device_id)
                for sequence in range(3):
                    self._send(
                        channel, _sdnk(3, bytes((1, sequence)) + media_id + media_id), remote
                    )
            elif kind == 4:
                channel.peer = remote
                self._send(channel, _sdnk(5, data[20:21] or b"\x01"), remote)
            elif kind == 5:
                channel.peer = remote
                channel.connected = True
                if all(item.connected for item in self.channels):
                    self._connected.set()
            return
        if control:
            self._handle_rtcp(data)
        else:
            self._handle_media(data)

    @property
    def transport_kind(self) -> str:
        """Describe observed peers without exposing addresses."""
        peers = [channel.peer for channel in self.channels]
        if len(peers) != 2 or any(peer is None for peer in peers):
            return "connecting"
        if all(peer is not None and peer[0] == self.camera.local_ip for peer in peers):
            try:
                _camera_lan_ip(self.camera.local_ip)
            except TendStreamError:
                pass
            else:
                return "lan"
        if all(peer is not None and peer[0] == self._relay_ip for peer in peers):
            return "cloud_relay"
        return "p2p"

    @property
    def statistics(self) -> dict[str, int]:
        """Return local counters; no network activity."""
        return {
            "rtp_packets": self._received,
            "media_discontinuities": self.media_discontinuities,
            "gop_recoveries": self.gop_recoveries,
        }

    def _invalidate_gop(self) -> None:
        self.media_discontinuities += 1
        self._recovering_gop = True
        self._waiting_idr = True

    def _schedule_fragment_expiry(self) -> None:
        deadline = self._assembler.deadline
        if deadline is None:
            if self._fragment_timer is not None:
                self._fragment_timer.cancel()
                self._fragment_timer = None
        elif self._fragment_timer is None:
            self._fragment_timer = asyncio.get_running_loop().call_later(
                max(0, deadline - time.monotonic()), self._expire_fragments
            )

    def _expire_fragments(self) -> None:
        self._fragment_timer = None
        for packet in self._assembler.expire(time.monotonic()):
            self._handle_rtp(packet)
        self._schedule_fragment_expiry()

    def _handle_media(self, data: bytes) -> None:
        now = time.monotonic()
        for packet in self._assembler.expire(now):
            self._handle_rtp(packet)
        for packet in self._assembler.feed(data, now):
            self._handle_rtp(packet)
        self._schedule_fragment_expiry()

    def _handle_rtp(self, packet: bytes) -> None:
        payload = rtp_payload(packet)
        if not payload:
            self._invalidate_gop()
            return
        source = struct.unpack_from(">I", packet, 8)[0]
        if self._source_ssrc is not None and self._source_ssrc != source:
            self._invalidate_gop()
            self._sps = self._pps = None
        # Count complete RTP packets even while dependent frames are withheld.
        self._observe_rtp(packet)
        records = decode_video_payload(payload, self.offer.aes_key)
        if not records:
            self._invalidate_gop()
            return
        for record in records:
            units = _nals(record)
            if not units:
                self._invalidate_gop()
                continue
            kinds = {kind for kind, _ in units}
            for kind, nal in units:
                if kind == 7:
                    self._sps = nal
                elif kind == 8:
                    self._pps = nal
            if self._waiting_idr:
                if 5 not in kinds or self._sps is None or self._pps is None:
                    continue
                prefix = (self._sps if 7 not in kinds else b"") + (
                    self._pps if 8 not in kinds else b""
                )
                record = prefix + record
                self._waiting_idr = False
                if self._recovering_gop:
                    self.gop_recoveries += 1
                    self._recovering_gop = False
            self.on_frame(record)

    def _observe_rtp(self, rtp: bytes) -> None:
        seq, timestamp, ssrc = struct.unpack_from(">HII", rtp, 2)
        if ssrc != self._source_ssrc:
            self._source_ssrc = ssrc
            self._base_seq = seq
            self._max_seq = seq
            self._cycles = 0
            self._received = 0
            self._expected_prior = 0
            self._received_prior = 0
            self._last_transit = None
            self._jitter = 0
        elif 0 < (seq - self._max_seq) % _RTP_SEQUENCE_MODULUS < _RTP_SEQUENCE_MODULUS // 2:
            if seq < self._max_seq:
                self._cycles += _RTP_SEQUENCE_MODULUS
            self._max_seq = seq
        self._received += 1
        transit = time.monotonic() * 90000 - timestamp
        if self._last_transit is not None:
            delta = abs(transit - self._last_transit)
            self._jitter += (delta - self._jitter) / 16
        self._last_transit = transit

    def _handle_rtcp(self, data: bytes) -> None:
        offset = 0
        while offset + 4 <= len(data):
            size = (struct.unpack_from(">H", data, offset + 2)[0] + 1) * 4
            if data[offset] >> 6 != 2 or size < 4 or offset + size > len(data):
                return
            if data[offset + 1] == 200 and size >= 20:
                msw, lsw = struct.unpack_from(">II", data, offset + 8)
                self._last_sr = ((msw & 0xFFFF) << 16) | (lsw >> 16)
                self._last_sr_at = time.monotonic()
            offset += size

    async def _report_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(2)
                report = self._receiver_report()
                if report is not None and self.channels[1].peer is not None:
                    self._send(self.channels[1], report, self.channels[1].peer)
        except asyncio.CancelledError:
            pass

    def _receiver_report(self) -> bytes | None:
        if self._source_ssrc is None:
            return None
        expected = self._cycles + self._max_seq - self._base_seq + 1
        lost = max(-(1 << 23), min((1 << 23) - 1, expected - self._received))
        expected_interval = expected - self._expected_prior
        lost_interval = expected_interval - (self._received - self._received_prior)
        fraction = (
            min(255, lost_interval * 256 // expected_interval)
            if expected_interval > 0 and lost_interval > 0
            else 0
        )
        self._expected_prior = expected
        self._received_prior = self._received
        report = bytearray(32)
        report[:2] = b"\x81\xc9"
        struct.pack_into(">HII", report, 2, 7, self._local_ssrc, self._source_ssrc)
        report[12] = fraction
        report[13:16] = (lost & 0xFFFFFF).to_bytes(3, "big")
        struct.pack_into(
            ">IIII",
            report,
            16,
            self._cycles + self._max_seq,
            int(self._jitter) & 0xFFFFFFFF,
            self._last_sr,
            min(int((time.monotonic() - self._last_sr_at) * 65536), 0xFFFFFFFF)
            if self._last_sr_at
            else 0,
        )
        return bytes(report)

    def close(self) -> None:
        if self._fragment_timer is not None:
            self._fragment_timer.cancel()
            self._fragment_timer = None
        self._assembler.clear()
        if self._report_task is not None:
            self._report_task.cancel()
            self._report_task = None
        if len(self.channels) == 2 and self.channels[1].peer is not None:
            bye = struct.pack(">BBHI", 0x81, 203, 1, self._local_ssrc)
            with contextlib.suppress(OSError):
                self._send(self.channels[1], bye, self.channels[1].peer)
                self._send(self.channels[1], bye, self.channels[1].peer)
        for channel in self.channels:
            channel.transport.close()
        self.channels.clear()
