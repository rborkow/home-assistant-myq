"""On-demand, TLS-relayed Video Keypad sessions with bounded wake-up."""

from __future__ import annotations

import asyncio
import contextlib
import json
import select
import struct
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from uuid import uuid4

from .client import _tend_headers
from .const import TEND_API_BASE_URL, TEND_PARTNER_ID
from .keypad_protocol import Session, login_routed, relay_access_units
from .keypad_state import KeypadPower
from .models import MyQCamera
from .tend_protocol import TendStreamError


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class KeypadSession:
    """Run blocking legacy TLS I/O outside HA's event loop; never store credentials."""

    def __init__(
        self,
        on_frame: Callable[[bytes], None],
        on_power: Callable[[KeypadPower], None] | None = None,
        on_phase: Callable[[str], None] | None = None,
        on_finished: Callable[[str | None], None] | None = None,
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._on_frame = on_frame
        self._on_power = on_power
        self._on_phase = on_phase
        self._on_finished = on_finished
        self._stop = threading.Event()
        self._ready: asyncio.Future[None] = self._loop.create_future()
        self._worker: asyncio.Task[None] | None = None
        self._control: Session | None = None
        self._media: Session | None = None
        self.ended = False
        self._device_info: dict[str, object] = {}

    @classmethod
    async def open(
        cls,
        camera: MyQCamera,
        token: str,
        on_frame: Callable[[bytes], None],
        *,
        on_power: Callable[[KeypadPower], None] | None = None,
        on_phase: Callable[[str], None] | None = None,
        on_finished: Callable[[str | None], None] | None = None,
    ) -> KeypadSession:
        session = cls(on_frame, on_power, on_phase, on_finished)
        session._worker = asyncio.create_task(asyncio.to_thread(session._run, camera, token))
        try:
            async with asyncio.timeout(100):
                await asyncio.shield(session._ready)
        except BaseException:
            await session.close()
            raise
        return session

    def _started(self) -> None:
        if not self._ready.done():
            self._ready.set_result(None)

    def _finished(self, error: str | None) -> None:
        self.ended = True
        if not self._ready.done():
            self._ready.set_exception(TendStreamError(error or "Keypad session closed"))
        if self._on_finished is not None:
            self._on_finished(error)
        self._on_frame(b"")

    def _phase(self, stage: str) -> None:
        if self._on_phase is not None:
            self._loop.call_soon_threadsafe(self._on_phase, stage)

    def _publish_power(self, device: object) -> None:
        if self._on_power is None or not isinstance(device, dict):
            return
        try:
            power = KeypadPower.from_device(device)
        except ValueError:
            return
        self._loop.call_soon_threadsafe(self._on_power, power)

    def _wake(self, camera: MyQCamera, token: str) -> None:
        if self._stop.is_set():
            raise TendStreamError("Keypad session cancelled before wake")
        device_id = camera.tend_device_id
        if not device_id.isdecimal():
            raise TendStreamError("Invalid keypad video identifier")
        endpoint = f"{TEND_API_BASE_URL}/devices/{device_id}"
        headers = {**_tend_headers(token), "Content-Type": "application/json"}
        opener = urllib.request.build_opener(_NoRedirect)
        query = urllib.parse.urlencode({"timestamp": int(time.time() * 1000)})
        with opener.open(
            urllib.request.Request(
                f"{endpoint}/do/wakeup?{query}", data=b"{}", headers=headers, method="POST"
            ),
            timeout=15,
        ) as response:
            response.read(65536)
        deadline = time.monotonic() + 35
        while not self._stop.is_set() and time.monotonic() < deadline:
            query = urllib.parse.urlencode({"timestamp": int(time.time() * 1000)})
            with opener.open(
                urllib.request.Request(f"{endpoint}?{query}", headers=headers), timeout=10
            ) as response:
                device = json.loads(response.read(1_024_000))
            self._device_info = device
            self._publish_power(device)
            if device.get("status", {}).get("pwr", {}).get("sleepMode") == "FP":
                return
            self._stop.wait(1)
        raise TendStreamError("Keypad did not reach full power")

    def _resolve_alias(self, camera: MyQCamera, token: str) -> str:
        """Resolve video-cloud identity on demand; account inventory can omit its MAC."""
        if not camera.tend_device_id.isdecimal():
            raise TendStreamError("Invalid keypad video identifier")
        request = urllib.request.Request(
            f"{TEND_API_BASE_URL}/devices/{camera.tend_device_id}",
            headers=_tend_headers(token),
        )
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=15) as response:
            device = json.loads(response.read(1_024_000))
        self._device_info = device
        self._publish_power(device)
        alias = device.get("alias")
        if not isinstance(alias, str) or ":" not in alias or any(c in alias for c in "\r\n|"):
            raise TendStreamError("Video cloud returned no valid keypad alias")
        return alias

    def _run(self, camera: MyQCamera, token: str) -> None:
        control = media = None
        requested = False
        alias = destination = ""
        correlation = str(uuid4())
        error = None
        stage = "identity"
        self._phase(stage)
        try:
            if self._stop.is_set():
                return
            resolved_alias = self._resolve_alias(camera, token)
            if self._stop.is_set():
                return
            stage = "login"
            self._phase(stage)
            control, props = login_routed(
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
            alias = props["alias"]
            destination = keypad_destination(props, camera, resolved_alias)
            stage = "relay_connect"
            self._phase(stage)
            media = Session(
                props["video-channel-host"],
                int(props["video-channel-port-tls"]),
                15,
                "server.tend-us.tendplatform.com",
                True,
            )
            self._media = media
            if self._stop.is_set():
                return
            media.sender, media.recipient = control.sender, control.recipient
            media.send(508, service=122)
            stage = "wake"
            self._phase(stage)
            self._wake(camera, token)
            if self._stop.is_set():
                return
            stage = "negotiate"
            self._phase(stage)
            control.command(f"require-video-send|{alias}|0|", destination, correlation)
            requested = True
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not self._stop.is_set():
                control.socket.settimeout(max(0.1, deadline - time.monotonic()))
                packet = control.receive()
                if packet.action == 438 and packet.data.decode().split("|")[:3] == [
                    "accept-video-receive",
                    destination,
                    "0",
                ]:
                    break
            else:
                raise TendStreamError("Keypad video negotiation timed out")
            media.send(508, service=122)
            stage = "relay_initialize"
            self._phase(stage)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                media.socket.settimeout(max(0.1, deadline - time.monotonic()))
                packet = media.receive()
                if packet.action == 508:
                    break
                if packet.action != 526:
                    raise TendStreamError("Unexpected keypad relay response")
            else:
                raise TendStreamError("Keypad relay initialization timed out")
            control.command(f"start-video-receive|{alias}|0|", destination, correlation)
            stage = "streaming"
            self._phase(stage)
            self._loop.call_soon_threadsafe(self._started)
            next_keepalive = time.monotonic() + 30
            last_frame = time.monotonic()
            quality_set = False
            while not self._stop.is_set():
                now = time.monotonic()
                if now - last_frame > 25:
                    raise TendStreamError("Keypad media stalled")
                if now >= next_keepalive:
                    control.send(526)
                    media.send(526)
                    next_keepalive = now + 30
                sockets = [control.socket, media.socket]
                ready = [sock for sock in sockets if sock.pending()]
                if not ready:
                    ready, _, _ = select.select(sockets, [], [], 1)
                for sock in ready:
                    sock.settimeout(10)
                    packet = (control if sock is control.socket else media).receive()
                    if sock is not media.socket or packet.action not in (413, 414):
                        continue
                    for unit in relay_access_units(packet.data, destination):
                        last_frame = time.monotonic()
                        self._loop.call_soon_threadsafe(self._on_frame, unit)
                        if not quality_set:
                            control.command(
                                f"modify-device|{destination}|QTY=4+FPS=20+SIZE=0+SAVE=0|",
                                destination,
                                correlation,
                            )
                            quality_set = True
        except Exception as exc:
            # Network exceptions can include URLs or account data. Never expose their messages.
            if not self._stop.is_set():
                error = f"Keypad {stage} failed ({type(exc).__name__})"
        finally:
            if control is not None:
                with contextlib.suppress(Exception):
                    if requested:
                        control.command(f"stop-video-receive|{alias}|0|", destination, correlation)
                    control.send(408, options=struct.pack("<I", 0))
                control.close()
            if media is not None:
                media.close()
            self._loop.call_soon_threadsafe(self._finished, error)

    async def close(self) -> None:
        self._stop.set()
        self.ended = True
        if self._worker is not None:
            await asyncio.shield(self._worker)
        # Retrieve an unobserved startup exception when the caller was cancelled.
        if self._ready.done() and not self._ready.cancelled():
            self._ready.exception()


def keypad_destination(
    properties: dict[str, str], camera: MyQCamera, resolved_alias: str | None = None
) -> str:
    """Match the selected camera, never fall back to the first account camera."""
    expected_mac = (camera.mac_address or "").replace(":", "").replace("-", "").lower()
    for line in properties.get("device-info-list", "").splitlines():
        destination, separator, _ = line.partition(",")
        if not separator or ":" not in destination:
            continue
        if resolved_alias is not None:
            if destination == resolved_alias:
                return destination
            continue
        device_id, _, mac = destination.partition(":")
        normalized_mac = mac.replace(":", "").replace("-", "").lower()
        if (expected_mac and normalized_mac == expected_mac) or device_id == camera.tend_device_id:
            return destination
    raise TendStreamError("Selected keypad was not listed by the video broker")
