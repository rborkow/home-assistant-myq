"""A passive, persistent preview built only from already received keypad video."""

from __future__ import annotations

import asyncio
import base64
import binascii
import math
import time
from datetime import UTC, datetime

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.setup import async_setup_component

_MAX_FRAME = 3_000_000


class KeypadPreview:
    """Keep a bounded keyframe; viewing the preview never starts a camera session."""

    def __init__(self, hass: HomeAssistant, camera_serial: str) -> None:
        self.hass = hass
        self._store = Store[dict[str, str | float]](
            hass, 1, f"myq.{camera_serial}.preview", private=True
        )
        self._frame: bytes | None = None
        self._frame_at: float | None = None
        self._image_frame: bytes | None = None
        self._image: bytes | None = None
        self._lock = asyncio.Lock()

    @property
    def captured_at(self) -> str | None:
        return datetime.fromtimestamp(self._frame_at, UTC).isoformat() if self._frame_at else None

    def received(self, frame: bytes) -> None:
        if not frame or len(frame) > _MAX_FRAME:
            return
        self._frame = frame
        self._frame_at = time.time()
        # Debounce disk writes while viewers are active; unload also saves explicitly.
        self._store.async_delay_save(self._data, 30)

    def _data(self) -> dict[str, str | float]:
        if self._frame is None or self._frame_at is None:
            return {}
        return {"frame": base64.b64encode(self._frame).decode("ascii"), "at": self._frame_at}

    async def load(self) -> None:
        stored: object = await self._store.async_load() or {}
        if not isinstance(stored, dict):
            return
        frame, at = stored.get("frame"), stored.get("at")
        if (
            not isinstance(frame, str)
            or len(frame) > _MAX_FRAME * 4 // 3
            or not isinstance(at, (int, float))
            or isinstance(at, bool)
            or not math.isfinite(at)
            or not 0 < at <= time.time() + 300
        ):
            return
        try:
            decoded = base64.b64decode(frame, validate=True)
        except ValueError, binascii.Error:
            return
        if decoded and len(decoded) <= _MAX_FRAME:
            self._frame, self._frame_at = decoded, float(at)

    async def save(self) -> None:
        if self._frame is not None:
            await self._store.async_save(self._data())

    async def image(self) -> bytes | None:
        async with self._lock:
            frame = self._frame
            if frame is None or frame == self._image_frame:
                return self._image
            # Ordinary door control and a never-viewed keypad do not need FFmpeg setup.
            if not await async_setup_component(self.hass, "ffmpeg", {}):
                return self._image
            process = await asyncio.create_subprocess_exec(
                get_ffmpeg_manager(self.hass).binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "h264",
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                async with asyncio.timeout(10):
                    image, _ = await process.communicate(frame)
                if process.returncode == 0 and image.startswith(b"\xff\xd8"):
                    self._image, self._image_frame = image, frame
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            return self._image
