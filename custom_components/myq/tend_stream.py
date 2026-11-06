"""Share a camera session through an aiohttp loopback video source."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from aiohttp import ClientError, web

from .exceptions import MyQError
from .tend_protocol import TendStreamError, _nals
from .tend_session import TendSession

_LOGGER = logging.getLogger(__name__)
_IDLE_SECONDS = 60
_FRAME_TIMEOUT = 30
_SHUTDOWN_TIMEOUT = 0.1


class _Viewer:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=60)
        self.ready = False
        self.ended = False

    def put(self, frame: bytes) -> None:
        if self.ended:
            return
        if self.frames.full():
            # A missing H.264 reference frame corrupts the rest of its GOP.
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
        lan_only: bool = False,
        idle_seconds: float = _IDLE_SECONDS,
        on_snapshot: Callable[[bytes], None] | None = None,
        prepare_session: Callable[[], Awaitable[None]] | None = None,
        on_diagnostic: Callable[[str, str | int | float | None], None] | None = None,
    ) -> None:
        self._on_diagnostic = on_diagnostic
        self._started_at: float | None = None
        self._first_frame = False
        self._failed = False
        self._prepare_session = prepare_session
        self._prepare_task: asyncio.Task[None] | None = None
        self._idle_seconds = idle_seconds
        self._on_snapshot = on_snapshot
        self._snapshot_frame: bytes | None = None
        self._opener_serial = opener_serial
        self._lan_only = lan_only
        self._token_supplier = token_supplier
        self._runner: web.AppRunner | None = None
        self._url: str | None = None
        self._server_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()
        self._session: TendSession | None = None
        self._viewers: set[_Viewer] = set()
        self._sps: bytes | None = None
        self._pps: bytes | None = None
        self._idle_timer: asyncio.TimerHandle | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def snapshot_frame(self) -> bytes | None:
        """Return the latest complete keyframe without opening a camera session."""
        return self._snapshot_frame

    async def source_url(self) -> str:
        """Start a loopback-only server with an unguessable stream path."""
        async with self._server_lock:
            if bool(self._closed):
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

    def _diagnostic(self, event: str, value: str | int | float | None = None) -> None:
        if self._on_diagnostic is not None:
            try:
                self._on_diagnostic(event, value)
            except Exception as error:
                _LOGGER.debug("Camera diagnostic callback failed: %s", type(error).__name__)

    def _phase(self, phase: str) -> None:
        self._diagnostic("phase", phase)

    def _statistics(self) -> None:
        if self._session is not None:
            for key, value in self._session.statistics.items():
                self._diagnostic(key, value)

    async def _prepare(self) -> None:
        assert self._prepare_session is not None
        await self._prepare_session()

    async def _subscribe(self) -> _Viewer:
        async with self._session_lock:
            if bool(self._closed):
                raise TendStreamError("Camera stream is closed")
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
            if self._session is None or self._session.ended:
                await self._close_session()
                if bool(self._closed):
                    raise TendStreamError("Camera stream is closed")
                self._started_at = time.monotonic()
                self._first_frame = False
                self._failed = False
                self._diagnostic("start")
                try:
                    await self._open_session()
                except asyncio.CancelledError:
                    self._diagnostic("end")
                    raise
                except Exception as error:
                    if self._closed:
                        self._diagnostic("end")
                    else:
                        self._failed = True
                        self._diagnostic("error", type(error).__name__)
                    raise
            viewer = _Viewer()
            self._viewers.add(viewer)
            self._diagnostic("consumers", len(self._viewers))
            return viewer

    async def _open_session(self) -> None:
        if self._prepare_session is not None:
            self._phase("waking")
            self._prepare_task = asyncio.create_task(self._prepare())
            try:
                await self._prepare_task
            except ClientError, MyQError, OSError, TimeoutError:
                raise TendStreamError("Camera could not prepare for live video") from None
            finally:
                self._prepare_task = None
        if bool(self._closed):
            raise TendStreamError("Camera stream is closed")
        token = await self._token_supplier()
        if bool(self._closed):
            raise TendStreamError("Camera stream is closed")
        self._session = await TendSession.open(
            self._opener_serial,
            token,
            self._on_frame,
            lan_only=self._lan_only,
            **({"on_phase": self._phase} if self._on_diagnostic is not None else {}),
        )
        if bool(self._closed):
            await self._close_session()
            raise TendStreamError("Camera stream is closed")

    def _on_frame(self, frame: bytes) -> None:
        if not frame:
            if not self._closed and self._session is not None and not self._failed:
                self._failed = True
                self._diagnostic("error", "VideoEnded")
            for viewer in self._viewers:
                viewer.end()
            return
        units = _nals(frame)
        types = {kind for kind, _ in units}
        if not types:
            return
        if types & {1, 5} and not self._first_frame and self._started_at is not None:
            self._first_frame = True
            self._diagnostic("first_frame", round(time.monotonic() - self._started_at, 3))
            self._statistics()
        for kind, nal in units:
            if kind == 7:
                self._sps = nal
            elif kind == 8:
                self._pps = nal
        if 5 in types and self._sps is not None and self._pps is not None:
            snapshot = self._sps if 7 not in types else b""
            snapshot += (self._pps if 8 not in types else b"") + frame
            if len(snapshot) <= 3_000_000:
                self._snapshot_frame = snapshot
                self._statistics()
                if self._on_snapshot is not None:
                    try:
                        self._on_snapshot(snapshot)
                    except Exception as error:
                        _LOGGER.debug("Camera snapshot callback failed: %s", type(error).__name__)
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

    @asynccontextmanager
    async def frames(self) -> AsyncIterator[asyncio.Queue[bytes]]:
        """Lease the same session used by HLS; cancellation releases only this reader."""
        viewer = await self._subscribe()
        try:
            yield viewer.frames
        finally:
            self._unsubscribe(viewer)

    def _unsubscribe(self, viewer: _Viewer) -> None:
        self._viewers.discard(viewer)
        self._diagnostic("consumers", len(self._viewers))
        self._statistics()
        if not self._viewers and not self._closed:
            delay = 0 if self._session is not None and self._session.ended else self._idle_seconds
            self._idle_timer = asyncio.get_running_loop().call_later(delay, self._start_idle_close)

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

    def _start_idle_close(self) -> None:
        self._idle_timer = None
        self._idle_task = asyncio.create_task(self._close_if_idle())

    async def _close_if_idle(self) -> None:
        async with self._session_lock:
            if not self._viewers:
                await self._close_session()

    async def _close_session(self) -> None:
        if self._session is not None:
            self._statistics()
            await self._session.close()
            self._session = None
            self._diagnostic("end")
        self._sps = self._pps = None

    async def close(self) -> None:
        """Finish cleanup even when the caller cancels entity removal."""
        if self._shutdown_task is None:
            self._closed = True
            if self._prepare_task is not None:
                self._prepare_task.cancel()
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
