"""Standalone battery camera entities sharing the integration's OAuth lifecycle."""

from __future__ import annotations

import asyncio
import base64
import binascii
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import MyQDataUpdateCoordinator
from .keypad_recording import record_keypad
from .keypad_session import KeypadSession
from .keypad_state import KeypadPower, KeypadState
from .keypad_transport import DEFAULT_POLICY, open_transport
from .models import VideoKeypad
from .tend_protocol import TendStreamError
from .tend_stream import TendLiveStream, _nals


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
        self._restored_frame: bytes | None = None
        self._snapshot_store = Store[dict[str, str | float]](
            coordinator.hass, 1, f"myq.{self._serial}.snapshot"
        )
        self._image_lock = asyncio.Lock()
        self._record_lock = asyncio.Lock()
        self._record_task: asyncio.Task[object] | None = None
        self._last_record_started: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        stored = await self._snapshot_store.async_load() or {}
        frame, at = stored.get("frame"), stored.get("at")
        if isinstance(frame, str) and len(frame) <= 4_000_000 and isinstance(at, (float, int)):
            try:
                self._restored_frame = base64.b64decode(frame, validate=True)
                self.keypad_state.snapshot_at = float(at)
                self.coordinator.async_keypad_changed()
            except binascii.Error:
                pass

    async def _save_snapshot(self) -> None:
        frame = self._live.snapshot_frame if self._live else None
        if frame and self.keypad_state.snapshot_at:
            await self._snapshot_store.async_save(
                {
                    "frame": base64.b64encode(frame).decode("ascii"),
                    "at": self.keypad_state.snapshot_at,
                }
            )

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
        started = time.monotonic()
        state = self.keypad_state
        state.session_active = True
        state.last_error = None
        state.transport = "connecting"
        state.fallback_used = False
        first_frame = True

        def frame(data: bytes) -> None:
            nonlocal first_frame
            kinds = {kind for kind, _ in _nals(data)}
            if kinds & {1, 5} and first_frame:
                first_frame = False
                state.first_frame_seconds = round(time.monotonic() - started, 3)
                self.coordinator.async_keypad_changed()
            if 5 in kinds:
                state.snapshot_at = time.time()
            on_frame(data)

        entry = self.coordinator.config_entry
        policy = (
            str(entry.options.get("transport_policy", DEFAULT_POLICY)) if entry else DEFAULT_POLICY
        )
        return await open_transport(
            policy,
            keypad.camera,
            token,
            frame,
            on_power=self._power,
            on_phase=self._phase,
            on_finished=self._finished,
            on_fallback=self._fallback,
        )

    @property
    def keypad_state(self) -> KeypadState:
        return self.coordinator.keypad_state(self._serial)

    def _power(self, power: KeypadPower) -> None:
        self.keypad_state.power = power
        self.coordinator.async_keypad_changed()

    def _fallback(self) -> None:
        state = self.keypad_state
        state.session_active = True
        state.last_error = None
        state.fallback_used = True
        state.transport = "connecting"
        self.coordinator.async_keypad_changed()

    def _phase(self, phase: str) -> None:
        self.keypad_state.phase = phase
        if phase == "recovering_lan":
            self.keypad_state.lan_recoveries += 1
        if phase in {"streaming", "streaming_lan"}:
            self.keypad_state.transport = "lan" if phase == "streaming_lan" else "cloud_relay"
            self.keypad_state.last_transport = self.keypad_state.transport
        self.coordinator.async_keypad_changed()

    def _finished(self, error: str | None) -> None:
        state = self.keypad_state
        state.session_active = False
        state.transport = "idle"
        state.phase = "error" if error else "idle"
        state.last_error = error
        if error:
            state.session_errors += 1
            state.last_failure = error
            state.failure_at = time.time()
        self.coordinator.async_keypad_changed()
        if self.hass:
            self.hass.async_create_task(self._save_snapshot())

    def _consumers(self, count: int) -> None:
        self.keypad_state.active_consumers = count
        self.coordinator.async_keypad_changed()

    async def stream_source(self) -> str | None:
        if not self.available:
            return None
        if self._live is None:
            self._live = TendLiveStream(
                self._serial,
                self.coordinator.client.async_access_token,
                session_factory=self._open_session,
                idle_seconds=20,
                on_consumers=self._consumers,
            )
        return await self._live.source_url()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        del width, height
        async with self._image_lock:
            frame = (self._live.snapshot_frame if self._live else None) or self._restored_frame
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
                await self._save_snapshot()
                await self._live.close()
        finally:
            self._live = None
            if self.stream is not None:
                await self.stream.stop()
                self.stream = None
            await super().async_will_remove_from_hass()

    async def async_record_keypad(self, duration: int | None = None) -> None:
        """Record validated received video to HA's local media directory."""
        if self._record_lock.locked():
            raise HomeAssistantError("A keypad recording is already active")
        entry = self.coordinator.config_entry
        options = entry.options if entry else {}
        duration = duration if duration is not None else int(options.get("recording_duration", 30))
        cooldown = int(options.get("recording_cooldown", 60))
        if (
            self._last_record_started is not None
            and time.monotonic() - self._last_record_started < cooldown
        ):
            raise HomeAssistantError("Keypad recording cooldown is active")
        self._last_record_started = time.monotonic()
        async with self._record_lock:
            self._record_task = asyncio.current_task()
            self.keypad_state.recording_status = "recording"
            self.coordinator.async_keypad_changed()
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
                await self.coordinator.recordings.async_add(result.path)
                self.keypad_state.recording_status = "completed"
                self.keypad_state.last_clip = result.path
                self.keypad_state.last_clip_at = time.time()
                self.hass.bus.async_fire(
                    "myq_keypad_recording_finished",
                    {
                        "entity_id": self.entity_id,
                        "status": "completed",
                        **asdict(result),
                    },
                )
            except asyncio.CancelledError:
                self.keypad_state.recording_status = "cancelled"
                self.hass.bus.async_fire(
                    "myq_keypad_recording_finished",
                    {"entity_id": self.entity_id, "status": "cancelled"},
                )
                raise
            except (
                HomeAssistantError,
                OSError,
                ValueError,
                TendStreamError,
                TimeoutError,
            ) as error:
                self.keypad_state.recording_status = "failed"
                self.hass.bus.async_fire(
                    "myq_keypad_recording_finished",
                    {
                        "entity_id": self.entity_id,
                        "status": "failed",
                        "error_type": type(error).__name__,
                    },
                )
                raise HomeAssistantError("Keypad recording failed") from error
            finally:
                self._record_task = None
                self.coordinator.async_keypad_changed()

    async def async_cancel_keypad_recording(self) -> None:
        """Cancel this camera's recording without stopping another viewer."""
        task = self._record_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
