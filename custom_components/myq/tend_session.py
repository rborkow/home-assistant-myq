"""Own the signaling and transport resources of one camera session."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from .tend_media import VideoTransport
from .tend_protocol import (
    CameraTargetSupplier,
    TendCamera,
    TendCameraNotFoundError,
    TendCameraTarget,
    TendStreamError,
    VideoOffer,
    parse_cameras,
)
from .tend_signaling import CxsConnection, request_video

_LOGGER = logging.getLogger(__name__)
_PUNCH_TIMEOUT = 8
_RETRY_DELAY = 4
_STOP_TIMEOUT = 1


class TendSession:
    """A single camera connection; an empty frame signals that it ended."""

    def __init__(self, on_frame: Callable[[bytes], None], *, lan_only: bool = False) -> None:
        self._on_frame = on_frame
        self._lan_only = lan_only
        self._connection = CxsConnection(self._on_lost)
        self._camera: TendCamera | None = None
        self._offer: VideoOffer | None = None
        self._transport: VideoTransport | None = None
        self._close_lock = asyncio.Lock()
        self.ended = False

    @classmethod
    async def open(
        cls,
        target: TendCameraTarget,
        token: str,
        on_frame: Callable[[bytes], None],
        *,
        lan_only: bool = False,
        fallback_target_supplier: CameraTargetSupplier | None = None,
    ) -> TendSession:
        """Connect to the requested camera, releasing failed attempts."""
        session = cls(on_frame, lan_only=lan_only)
        ready = False
        try:
            await session._connection.login(token)
            cameras = parse_cameras(session._connection.properties)
            try:
                session._camera = target.select(cameras)
            except TendCameraNotFoundError:
                if fallback_target_supplier is None:
                    raise
                fallback = await fallback_target_supplier()
                session._camera = fallback.select(cameras)
            await session._start()
            if session.ended:
                raise TendStreamError("Tend signaling closed during camera setup")
            ready = True
            return session
        finally:
            if not ready:
                await session.close()

    async def _start(self) -> None:
        camera = self._camera
        assert camera is not None
        for attempt in range(2):
            self._offer = await request_video(self._connection, camera)
            self._transport = VideoTransport(
                camera, self._offer, self._on_frame, lan_only=self._lan_only
            )
            try:
                await self._transport.punch(_PUNCH_TIMEOUT)
            except TendStreamError:
                await self._stop_media()
                if attempt:
                    raise
                await asyncio.sleep(_RETRY_DELAY)
                continue
            await self._connection.command(
                f"start-video-receive|{camera.device_id}|9|",
                camera.destination,
                self._offer.correlation,
            )
            await self._connection.command(
                f"modify-device|{camera.device_id}|QTY=3+FPS=20+SIZE=0+SAVE=0|",
                camera.destination,
            )
            return

    def _on_lost(self, connection: CxsConnection) -> None:
        del connection
        self.ended = True
        self._on_frame(b"")

    async def _stop_media(self) -> None:
        try:
            if self._camera is not None and self._offer is not None:
                async with asyncio.timeout(_STOP_TIMEOUT):
                    await self._connection.command(
                        f"stop-video-receive|{self._camera.device_id}|9|",
                        self._camera.destination,
                        self._offer.correlation,
                    )
        except OSError, TimeoutError, TendStreamError:
            _LOGGER.debug("Tend camera remote stop was not acknowledged")
        finally:
            if self._transport is not None:
                self._transport.close()
                self._transport = None
            self._offer = None

    async def close(self) -> None:
        """Release local resources even if the remote stop is cancelled or fails."""
        async with self._close_lock:
            self.ended = True
            try:
                await self._stop_media()
            finally:
                await self._connection.aclose()
                self._camera = None
