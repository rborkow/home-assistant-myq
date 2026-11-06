"""Cloud-assisted signaling with an exclusively RFC1918 LAN media plane."""

from __future__ import annotations

import contextlib
import re
import select
import socket
import struct
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import uuid4

from .const import TEND_PARTNER_ID
from .keypad_lan_protocol import (
    LanPacketLoss,
    LanStreamError,
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
from .keypad_protocol import Session, login_routed
from .keypad_session import KeypadSession, keypad_destination
from .models import MyQCamera
from .tend_stream import _nals


def device_key(properties: dict[str, str], destination: str) -> bytes:
    line = next(
        (
            line
            for line in properties.get("device-info-list", "").splitlines()
            if line.partition(",")[0] == destination
        ),
        "",
    )
    values: list[str] = re.findall(r"(?:^|[+,])AES=([^+\n,]*)", line)
    if len(values) != 1 or len(values[0].encode()) != 16:
        raise ValueError("Selected keypad has no valid media key")
    return values[0].encode()


def rendezvous_endpoint(options: bytes) -> tuple[str, int]:
    value = urlsplit(options.decode("ascii").rstrip("\0"))
    if (
        value.scheme != "rtp"
        or not value.hostname
        or value.username
        or value.password
        or value.path
        or value.query
        or value.fragment
        or not value.port
        or not re.fullmatch(r"[A-Za-z0-9.-]+", value.hostname)
    ):
        raise ValueError("Invalid authenticated rendezvous endpoint")
    address = socket.gethostbyname(value.hostname)
    return address, value.port


@dataclass(slots=True)
class _Channel:
    number: int
    request: bytes
    peer: tuple[str, int] | None = None
    ready: bool = False
    counter: int = 0


class LanPeer:
    """Two bounded UDP channels. Public candidates are never selected or contacted."""

    def __init__(self, server: tuple[str, int], expected: str, viewer: str, device: str) -> None:
        self.server = server
        self.expected = private_ipv4(expected)
        self.viewer = identity(viewer)
        self.device = identity(device)
        self.identities = self.viewer + self.device
        self.channels: dict[socket.socket, _Channel] = {}
        self.reporter = ReceiverReports()
        self.reassembler = Reassembler()
        self.waiting_keyframe = True
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                route.connect((self.expected, 9))  # Select LAN interface without sending data.
                local = private_ipv4(route.getsockname()[0])
            if local == self.expected:
                raise ValueError("Camera LAN address points to this host")
            for number in range(2):
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.channels[sock] = _Channel(number, b"")
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2_097_152)
                sock.bind((local, 0))
                sock.setblocking(False)
                self.channels[sock].request = discovery(
                    local, sock.getsockname()[1], number, self.identities
                )
        except BaseException:
            self.close()
            raise

    @property
    def ready(self) -> bool:
        return bool(self.channels) and all(channel.ready for channel in self.channels.values())

    def handshake_tick(self) -> None:
        for sock, channel in self.channels.items():
            if channel.peer is None:
                sock.sendto(channel.request, self.server)
            elif not channel.ready:
                sock.sendto(command(3, bytes((1, channel.counter)) + self.identities), channel.peer)
                channel.counter = (channel.counter + 1) % 256

    def keepalive(self, now: float) -> None:
        for sock, channel in self.channels.items():
            if channel.ready and channel.peer:
                sock.sendto(command(5, b"\0"), channel.peer)
                if channel.number == 1:
                    sock.sendto(self.reporter.packet(now), channel.peer)

    def check_deadline(self, now: float) -> None:
        try:
            self.reassembler.check_deadline(now)
        except LanPacketLoss:
            self.reassembler.skip_gap()
            self.waiting_keyframe = True

    def receive(self, sock: socket.socket, key: bytes, now: float) -> list[bytes]:
        data, source = sock.recvfrom(2049)
        channel = self.channels[sock]
        # A broker response can introduce only the independently authenticated LAN IP.
        if source == self.server and not channel.ready:
            candidate = peer_candidate(data, self.expected)
            if candidate is not None:
                if channel.peer is not None and channel.peer != candidate:
                    raise LanStreamError("LAN peer changed during negotiation")
                channel.peer = candidate
                sock.sendto(
                    command(100, bytes((1, channel.number)) + b"V" + self.identities), self.server
                )
                sock.sendto(command(3, b"\1\0" + self.identities), candidate)
            return []
        if channel.peer is None or source != channel.peer:
            return []
        if data.startswith(b"SDNK"):
            kind = command_type(data)
            if kind == 3 and len(data) == 86:
                if data[22:54] == self.device and data[54:86] == self.viewer:
                    sock.sendto(command(4, data[21:22], data[8:12]), source)
            elif kind == 4 and len(data) == 21:
                sock.sendto(command(101, data[20:21], data[8:12]), source)
                channel.ready = True
            elif kind == 101 and len(data) == 21:
                channel.ready = True
            return []
        if not self.ready:
            return []
        if channel.number == 1:
            self.reporter.sender_report(data, now)
            return []
        frames: list[bytes] = []
        try:
            packets = self.reassembler.feed(data, now)
        except LanPacketLoss:
            self.reassembler.skip_gap()
            self.waiting_keyframe = True
            packets = self.reassembler.feed(data, now)
        for packet in packets:
            units = decrypt_media(packet.payload, key)
            self.reporter.received(packet, now)
            for unit in units:
                if self.waiting_keyframe:
                    if not {kind for kind, _ in _nals(unit)} & {5}:
                        continue
                    self.waiting_keyframe = False
                frames.append(unit)
        return frames

    def close(self) -> None:
        for sock in self.channels:
            sock.close()
        self.channels.clear()


class KeypadLanSession(KeypadSession):
    """Preserve the existing session API; never instantiate a cloud media socket."""

    def _run(self, camera: MyQCamera, token: str) -> None:
        control: Session | None = None
        peer: LanPeer | None = None
        requested = False
        alias = destination = ""
        correlation = str(uuid4())
        error = None
        stage = "identity"
        try:
            self._phase(stage)
            if self._stop.is_set():
                return
            resolved = self._resolve_alias(camera, token)
            stage = "wake"
            self._phase(stage)
            self._wake(camera, token)
            if self._stop.is_set():
                return
            net = self._device_info.get("net")
            ethernet = net.get("eth") if isinstance(net, dict) else None
            lan = private_ipv4(ethernet.get("lip") if isinstance(ethernet, dict) else None)
            stage = "login"
            self._phase(stage)
            control, properties = login_routed(
                {
                    "m_serverhost": "server.seedonk.com",
                    "mServerSSLPort": 5104,
                    "m_pwd": "myQ.accessToken:" + token,
                },
                TEND_PARTNER_ID,
                15,
                "server.tend-us.tendplatform.com",
                True,
            )
            self._control = control
            if self._stop.is_set():
                return
            alias = properties["alias"]
            destination = keypad_destination(properties, camera, resolved)
            key = device_key(properties, destination)
            stage = "lan_negotiate"
            self._phase(stage)
            control.command(f"require-video-send|{alias}|9|", destination, correlation)
            requested = True
            deadline = time.monotonic() + 20
            while not self._stop.is_set() and time.monotonic() < deadline:
                if (
                    not control.socket.pending()
                    and not select.select([control.socket], [], [], 0.2)[0]
                ):
                    continue
                control.socket.settimeout(3)
                packet = control.receive()
                if packet.action == 438 and packet.data.decode().split("|")[:3] == [
                    "accept-video-receive",
                    destination,
                    "9",
                ]:
                    peer = LanPeer(rendezvous_endpoint(packet.options), lan, alias, destination)
                    break
            if self._stop.is_set():
                return
            if peer is None:
                raise LanStreamError("LAN video negotiation timed out")
            stage = "lan_connect"
            self._phase(stage)
            deadline = time.monotonic() + 18
            next_handshake = 0.0
            next_keepalive = time.monotonic() + 5
            next_control = time.monotonic() + 30
            last_frame = time.monotonic()
            started = False
            quality_set = False
            while not self._stop.is_set():
                now = time.monotonic()
                if not started:
                    if now >= deadline:
                        raise LanStreamError("LAN peer handshake timed out")
                    if now >= next_handshake:
                        peer.handshake_tick()
                        next_handshake = now + 1
                    if peer.ready:
                        control.command(f"start-video-receive|{alias}|9|", destination, correlation)
                        started = True
                        last_frame = now
                        stage = "lan_start"
                        self._phase(stage)
                elif now - last_frame > 15:
                    raise LanStreamError("LAN video stalled")
                peer.check_deadline(now)
                if quality_set and peer.waiting_keyframe and stage != "recovering_lan":
                    stage = "recovering_lan"
                    self._phase(stage)
                if now >= next_keepalive:
                    peer.keepalive(now)
                    next_keepalive = now + 5
                if now >= next_control:
                    control.send(526)
                    next_control = now + 30
                ready = select.select([control.socket, *peer.channels], [], [], 0.1)[0]
                if control.socket.pending() and control.socket not in ready:
                    ready.append(control.socket)
                for sock in ready:
                    if sock is control.socket:
                        sock.settimeout(3)
                        control.receive()
                        continue
                    for frame in peer.receive(sock, key, time.monotonic()):
                        last_frame = time.monotonic()
                        if stage != "streaming_lan":
                            stage = "streaming_lan"
                            self._phase(stage)
                            self._loop.call_soon_threadsafe(self._started)
                        self._loop.call_soon_threadsafe(self._on_frame, frame)
                        if not quality_set:
                            control.command(
                                f"modify-device|{destination}|QTY=4+FPS=20+SIZE=0+SAVE=0|",
                                destination,
                                correlation,
                            )
                            quality_set = True
        except Exception as exc:
            if not self._stop.is_set():
                reason = str(exc) if isinstance(exc, LanStreamError) else type(exc).__name__
                error = f"Keypad {stage} failed ({reason})"
        finally:
            if peer is not None:
                peer.close()
            if control is not None:
                with contextlib.suppress(Exception):
                    if requested:
                        control.command(f"stop-video-receive|{alias}|9|", destination, correlation)
                    control.send(408, options=struct.pack("<I", 0))
                control.close()
            self._loop.call_soon_threadsafe(self._finished, error)
