"""Share a camera session through an aiohttp loopback video source."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Protocol

from aiohttp import ClientError, web

from .exceptions import MyQError
from .tend_protocol import TendStreamError
from .tend_session import TendSession

_LOGGER = logging.getLogger(__name__)
_IDLE_SECONDS = 60
_FRAME_TIMEOUT = 30
_SHUTDOWN_TIMEOUT = 0.1


class CameraSession(Protocol):
    ended: bool

    async def close(self) -> None: ...


type SessionFactory = Callable[[str, Callable[[bytes], None]], Awaitable[CameraSession]]


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


class _Viewer:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=60)
        self.ready = False
        self.ended = False

    def put(self, frame: bytes) -> None:
        if self.ended:
            return
        if self.frames.full():
            # Dropping arbitrary H.264 frames corrupts the rest of the GOP.
            # End only this slow consumer; it may reconnect at a fresh keyframe.
            while not self.frames.empty():
                self.frames.get_nowait()
            self.frames.put_nowait(b"")
            self.ended = True
            return
        self.frames.put_nowait(frame)

    def end(self) -> None:
        self.put(b"")
        self.ended = True


class TendLiveStream:
    """Expose one TC camera session to multiple local stream consumers."""

    def __init__(
        self,
        opener_serial: str,
        token_supplier: Callable[[], Awaitable[str]],
        *,
        session_factory: SessionFactory | None = None,
        idle_seconds: float | None = None,
        on_consumers: Callable[[int], None] | None = None,
    ) -> None:
        self._opener_serial = opener_serial
        self._token_supplier = token_supplier
        self._session_factory = session_factory
        self._idle_seconds = idle_seconds
        self._on_consumers = on_consumers
        self._runner: web.AppRunner | None = None
        self._url: str | None = None
        self._server_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()
        self._session: CameraSession | None = None
        self._viewers: set[_Viewer] = set()
        self._sps: bytes | None = None
        self._pps: bytes | None = None
        self.snapshot_frame: bytes | None = None
        self._idle_timer: asyncio.TimerHandle | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._closed = False

    async def source_url(self) -> str:
        """Start a loopback-only server with an unguessable stream path."""
        async with self._server_lock:
            if self._closed:
                raise TendStreamError("Camera stream is closed")
            if self._url is None:
                path = "/" + secrets.token_urlsafe(24) + ".h264"
                app = web.Application()
                app.router.add_get(path, self._handle_client)
                runner = web.AppRunner(
                    app,
                    access_log=None,
                    handler_cancellation=True,
                    shutdown_timeout=_SHUTDOWN_TIMEOUT,
                )
                try:
                    await runner.setup()
                    site = web.TCPSite(runner, "127.0.0.1", 0)
                    await site.start()
                    self._url = f"{site.name}{path}"
                    self._runner = runner
                finally:
                    if self._url is None:
                        await runner.cleanup()
            return self._url

    async def _subscribe(self) -> _Viewer:
        async with self._session_lock:
            if self._closed:
                raise TendStreamError("Camera stream is closed")
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
            if self._session is None or self._session.ended:
                await self._close_session()
                token = await self._token_supplier()
                if self._session_factory is None:
                    self._session = await TendSession.open(
                        self._opener_serial, token, self._on_frame
                    )
                else:
                    self._session = await self._session_factory(token, self._on_frame)
            viewer = _Viewer()
            self._viewers.add(viewer)
            if self._on_consumers is not None:
                self._on_consumers(len(self._viewers))
            return viewer

    def _on_frame(self, frame: bytes) -> None:
        if not frame:
            for viewer in self._viewers:
                viewer.end()
            return
        units = _nals(frame)
        types = {kind for kind, _ in units}
        if not types:
            return
        for kind, nal in units:
            if kind == 7:
                self._sps = nal
            elif kind == 8:
                self._pps = nal
        if 5 in types and self._sps is not None and self._pps is not None:
            self.snapshot_frame = (
                (b"" if 7 in types else self._sps) + (b"" if 8 in types else self._pps) + frame
            )
        for viewer in self._viewers:
            if not viewer.ready:
                if 5 not in types or self._sps is None or self._pps is None:
                    continue
                if 7 not in types:
                    viewer.put(self._sps)
                if 8 not in types:
                    viewer.put(self._pps)
                viewer.ready = True
            viewer.put(frame)

    async def _handle_client(self, request: web.Request) -> web.StreamResponse:
        viewer: _Viewer | None = None
        response = web.StreamResponse(
            headers={"Content-Type": "video/h264", "Cache-Control": "no-store"}
        )
        try:
            viewer = await self._subscribe()
            await response.prepare(request)
            while True:
                try:
                    async with asyncio.timeout(_FRAME_TIMEOUT):
                        frame = await viewer.frames.get()
                except TimeoutError as error:
                    if self._session is not None:
                        self._session.ended = True
                    self._on_frame(b"")
                    raise TendStreamError("Camera stopped sending video") from error
                if not frame:
                    break
                async with asyncio.timeout(_FRAME_TIMEOUT):
                    await response.write(frame)
        except (ConnectionError, TimeoutError) as error:
            _LOGGER.debug("Tend stream consumer disconnected: %s", type(error).__name__)
            if not response.prepared:
                raise web.HTTPServiceUnavailable() from error
        except (ClientError, MyQError, OSError, TendStreamError, ValueError) as error:
            _LOGGER.warning(
                "TC camera live video unavailable: %s",
                str(error) if isinstance(error, TendStreamError) else type(error).__name__,
            )
            if not response.prepared:
                raise web.HTTPServiceUnavailable() from error
        finally:
            if viewer is not None:
                self._unsubscribe(viewer)
        return response

    @asynccontextmanager
    async def frames(self) -> AsyncIterator[asyncio.Queue[bytes]]:
        """Lease the shared session for recording; empty bytes mark discontinuity."""
        viewer = await self._subscribe()
        try:
            yield viewer.frames
        finally:
            self._unsubscribe(viewer)

    def _unsubscribe(self, viewer: _Viewer) -> None:
        self._viewers.discard(viewer)
        if self._on_consumers is not None:
            self._on_consumers(len(self._viewers))
        if not self._viewers and not self._closed:
            delay = _IDLE_SECONDS if self._idle_seconds is None else self._idle_seconds
            if self._session is not None and self._session.ended:
                delay = 0
            if self._idle_timer is not None:
                self._idle_timer.cancel()
            self._idle_timer = asyncio.get_running_loop().call_later(delay, self._start_idle_close)

    def _start_idle_close(self) -> None:
        self._idle_timer = None
        self._idle_task = asyncio.create_task(self._close_if_idle())

    async def _close_if_idle(self) -> None:
        async with self._session_lock:
            if not self._viewers:
                await self._close_session()

    async def _close_session(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._sps = self._pps = None

    async def close(self) -> None:
        """Finish cleanup even when the caller cancels entity removal."""
        if self._shutdown_task is None:
            self._closed = True
            self._shutdown_task = asyncio.create_task(self._shutdown())
        try:
            await asyncio.shield(self._shutdown_task)
        except asyncio.CancelledError:
            await self._shutdown_task
            raise

    async def _shutdown(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        self._on_frame(b"")
        try:
            async with self._server_lock:
                if self._runner is not None:
                    await self._runner.cleanup()
                    self._runner = None
        finally:
            if self._idle_task is not None:
                await self._idle_task
            async with self._session_lock:
                await self._close_session()
