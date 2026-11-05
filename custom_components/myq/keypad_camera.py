"""Standalone battery camera entities sharing the integration's OAuth lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import MyQDataUpdateCoordinator
from .keypad_recording import record_keypad
from .keypad_session import KeypadSession
from .models import VideoKeypad
from .tend_protocol import TendStreamError
from .tend_stream import TendLiveStream


class MyQKeypadCamera(CoordinatorEntity[MyQDataUpdateCoordinator], Camera):
    """Wake only for explicit live consumers; snapshots use already received frames."""

    _attr_has_entity_name = True
    _attr_name = "Camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: MyQDataUpdateCoordinator, keypad: VideoKeypad) -> None:
        super().__init__(coordinator)
        Camera.__init__(self)
        self._serial = keypad.camera.serial_number
        self._attr_unique_id = f"{self._serial}_keypad_camera"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=keypad.name,
            manufacturer=MANUFACTURER,
            model="Video Keypad",
            serial_number=self._serial,
        )
        self.stream_options = {"use_wallclock_as_timestamps": True}
        self._live: TendLiveStream | None = None
        self._image: bytes | None = None
        self._image_frame: bytes | None = None
        self._image_lock = asyncio.Lock()
        self._record_lock = asyncio.Lock()
        self._record_task: asyncio.Task[object] | None = None

    @property
    def keypad(self) -> VideoKeypad | None:
        return next(
            (
                item
                for item in self.coordinator.client.keypads
                if item.camera.serial_number == self._serial
            ),
            None,
        )

    @property
    def available(self) -> bool:
        return (
            super().available and self.keypad is not None and self.keypad.camera.online is not False
        )

    async def _open_session(self, token: str, on_frame: Callable[[bytes], None]) -> KeypadSession:
        keypad = self.keypad
        if keypad is None:
            raise TendStreamError("Keypad is no longer present")
        return await KeypadSession.open(keypad.camera, token, on_frame)

    async def stream_source(self) -> str | None:
        if not self.available:
            return None
        if self._live is None:
            self._live = TendLiveStream(
                self._serial,
                self.coordinator.client.async_access_token,
                session_factory=self._open_session,
                idle_seconds=20,
            )
        return await self._live.source_url()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        del width, height
        async with self._image_lock:
            frame = self._live.snapshot_frame if self._live else None
            if frame is None or frame == self._image_frame:
                return self._image
            binary = get_ffmpeg_manager(self.hass).binary
            process = await asyncio.create_subprocess_exec(
                binary,
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

    async def async_will_remove_from_hass(self) -> None:
        if self._record_task is not None:
            self._record_task.cancel()
            await asyncio.gather(self._record_task, return_exceptions=True)
        try:
            if self._live is not None:
                await self._live.close()
        finally:
            self._live = None
            if self.stream is not None:
                await self.stream.stop()
                self.stream = None
            await super().async_will_remove_from_hass()

    async def async_record_keypad(self, duration: int = 30) -> None:
        """Record validated received video to HA's local media directory."""
        if self._record_lock.locked():
            raise HomeAssistantError("A keypad recording is already active")
        async with self._record_lock:
            self._record_task = asyncio.current_task()
            try:
                if await self.stream_source() is None or self._live is None:
                    raise HomeAssistantError("Keypad camera is unavailable")
                media_root = self.hass.config.media_dirs.get(
                    "local", self.hass.config.path("media")
                )
                result = await record_keypad(
                    self._live,
                    duration,
                    Path(media_root) / "myq-keypad",
                    get_ffmpeg_manager(self.hass).binary,
                )
                self.hass.bus.async_fire(
                    "myq_keypad_recording_finished",
                    {
                        "entity_id": self.entity_id,
                        "status": "completed",
                        **asdict(result),
                    },
                )
            except (OSError, ValueError, TendStreamError, TimeoutError) as error:
                self.hass.bus.async_fire(
                    "myq_keypad_recording_finished",
                    {
                        "entity_id": self.entity_id,
                        "status": "failed",
                        "error_type": type(error).__name__,
                    },
                )
                raise HomeAssistantError("Keypad recording failed validation") from error
            finally:
                self._record_task = None
