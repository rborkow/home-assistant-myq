"""Snapshots and live video from MyQ cameras.

Cameras attached to a garage door join that door's device. Cameras attached to no
door (such as the Video Keypad) get a device of their own.
"""

import asyncio
import time
from dataclasses import asdict

import voluptuous as vol
from aiohttp import ClientError
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.setup import async_setup_component

from .const import CONF_CAMERA_LAN_ONLY, DOMAIN, MANUFACTURER
from .coordinator import MyQDataUpdateCoordinator
from .entity import MyQEntity
from .exceptions import MyQError
from .keypad_preview import KeypadPreview
from .keypad_recording import record_keypad
from .models import GarageDoor, MyQCamera
from .runtime import MyQConfigEntry
from .tend_protocol import TendStreamError
from .tend_stream import TendLiveStream

SNAPSHOT_FRAME_INTERVAL = 5.0
VIDEO_KEYPAD_MODEL = "vkp-camera"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyQConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add attached cameras to their doors and unattached cameras on their own."""
    del hass
    coordinator = entry.runtime_data.coordinator
    entities: list[Camera] = [
        MyQGarageCamera(coordinator, door)
        for door in coordinator.data.values()
        if door.camera is not None
    ]
    entities.extend(
        MyQStandaloneCamera(coordinator, camera) for camera in coordinator.cameras.values()
    )
    async_add_entities(entities)
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "record_keypad",
        {vol.Optional("duration"): vol.All(vol.Coerce(int), vol.Range(min=5, max=300))},
        "async_record_keypad",
    )
    platform.async_register_entity_service(
        "cancel_keypad_recording", {}, "async_cancel_keypad_recording"
    )


class _MyQCameraMixin(Camera):
    """Snapshot sharing and TC live video, common to both camera kinds."""

    _attr_frame_interval = SNAPSHOT_FRAME_INTERVAL
    coordinator: MyQDataUpdateCoordinator

    def _init_camera(self, camera: MyQCamera | None, opener_serial: str) -> None:
        Camera.__init__(self)
        self._preview = (
            KeypadPreview(self.coordinator.hass, camera.serial_number)
            if camera is not None and camera.device_model == VIDEO_KEYPAD_MODEL
            else None
        )
        self._opener_serial = opener_serial
        self._record_lock = asyncio.Lock()
        self._record_task: asyncio.Task[object] | None = None
        self._last_record_started: float | None = None
        self._capture_lock = asyncio.Lock()
        self._image: bytes | None = None
        self._captured_at = 0.0
        self._live_stream: TendLiveStream | None = None
        if camera is not None and camera.serial_prefix == "TC":
            self._attr_supported_features = CameraEntityFeature.STREAM
            self.stream_options = {"use_wallclock_as_timestamps": True}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._preview is not None:
            await self._preview.load()

    def _received_snapshot(self, frame: bytes) -> None:
        if self._preview is not None:
            self._preview.received(frame)
            if self.hass and self.entity_id:
                self.async_write_ha_state()

    @property
    def camera(self) -> MyQCamera | None:
        raise NotImplementedError

    @property
    def model(self) -> str | None:
        return self.camera.device_model if self.camera is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, str | None] | None:
        camera = self.camera
        if camera is None or camera.serial_prefix is None:
            return None
        attributes: dict[str, str | None] = {"camera_serial_prefix": camera.serial_prefix}
        if self._preview is not None:
            attributes["preview_updated"] = self._preview.captured_at
        return attributes

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Capture one JPEG, sharing the result with concurrent callers."""
        del width, height
        camera = self.camera
        if camera is None:
            return None
        if self._preview is not None:
            return await self._preview.image()
        requested_at = time.monotonic()
        async with self._capture_lock:
            if self._image is not None and self._captured_at > requested_at:
                return self._image
            try:
                self._image = await self.coordinator.client.async_get_camera_image(camera)
            except (ClientError, MyQError, TimeoutError) as error:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="camera_image_failed"
                ) from error
            self._captured_at = time.monotonic()
            return self._image

    async def stream_source(self) -> str | None:
        """Provide a local H.264 source for TC cameras."""
        camera = self.camera
        if not self.available or camera is None or camera.serial_prefix != "TC":
            return None
        if self._live_stream is None:
            entry = self.coordinator.config_entry
            lan_only = bool(entry.options.get(CONF_CAMERA_LAN_ONLY, False)) if entry else False
            if self._preview is not None:
                self._live_stream = TendLiveStream(
                    self._opener_serial,
                    self.coordinator.client.async_access_token,
                    lan_only=lan_only,
                    idle_seconds=20,
                    on_snapshot=self._received_snapshot,
                )
            else:
                self._live_stream = TendLiveStream(
                    self._opener_serial,
                    self.coordinator.client.async_access_token,
                    lan_only=lan_only,
                )
        return await self._live_stream.source_url()

    async def async_will_remove_from_hass(self) -> None:
        """Close the source before waiting for the stream reader to stop."""
        try:
            await self.async_cancel_keypad_recording()
            if self._live_stream is not None:
                await self._live_stream.close()
        finally:
            try:
                if self._preview is not None:
                    await self._preview.save()
            finally:
                self._live_stream = None
                if self.stream is not None:
                    await self.stream.stop()
                    self.stream = None
                await super().async_will_remove_from_hass()

    async def async_record_keypad(self, duration: int | None = None) -> None:
        """Record received keypad video with the existing shared camera session."""
        camera = self.camera
        if camera is None or camera.device_model != VIDEO_KEYPAD_MODEL:
            raise HomeAssistantError("Select a Video Keypad camera")
        if self._record_lock.locked():
            raise HomeAssistantError("A keypad recording is already active")
        entry = self.coordinator.config_entry
        options = entry.options if entry else {}
        duration = duration if duration is not None else int(options.get("recording_duration", 30))
        if isinstance(duration, bool) or not 5 <= duration <= 300:
            raise HomeAssistantError("Recording duration must be 5 to 300 seconds")
        cooldown = int(options.get("recording_cooldown", 60))
        if (
            self._last_record_started is not None
            and time.monotonic() - self._last_record_started < cooldown
        ):
            raise HomeAssistantError("Keypad recording cooldown is active")
        self._last_record_started = time.monotonic()
        async with self._record_lock:
            self._record_task = asyncio.current_task()
            try:
                if await self.stream_source() is None or self._live_stream is None:
                    raise TendStreamError("Keypad live stream is unavailable")
                if not await async_setup_component(self.hass, "ffmpeg", {}):
                    raise TendStreamError("FFmpeg setup failed")
                result = await record_keypad(
                    self._live_stream,
                    duration,
                    self.coordinator.recordings.root,
                    get_ffmpeg_manager(self.hass).binary,
                    enroll=self.coordinator.recordings.async_add,
                )
                self.hass.bus.async_fire(
                    "myq_keypad_recording_finished",
                    {"entity_id": self.entity_id, "status": "completed", **asdict(result)},
                )
            except asyncio.CancelledError:
                self.hass.bus.async_fire(
                    "myq_keypad_recording_finished",
                    {"entity_id": self.entity_id, "status": "cancelled"},
                )
                raise
            except (OSError, TendStreamError, TimeoutError, ValueError) as error:
                self.hass.bus.async_fire(
                    "myq_keypad_recording_finished",
                    {
                        "entity_id": self.entity_id,
                        "status": "failed",
                        "error": type(error).__name__,
                    },
                )
                raise HomeAssistantError("Keypad recording failed") from error
            finally:
                self._record_task = None

    async def async_cancel_keypad_recording(self) -> None:
        """Cancel the recording consumer, preserving other live viewers."""
        task = self._record_task
        if task is not None and task is not asyncio.current_task():
            if not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class MyQGarageCamera(MyQEntity, _MyQCameraMixin):
    """A camera attached to a garage door, on that door's device."""

    _attr_translation_key = "camera"

    def __init__(self, coordinator: MyQDataUpdateCoordinator, door: GarageDoor) -> None:
        super().__init__(coordinator, door)
        self._attr_unique_id = f"{door.serial_number}_camera"
        self._init_camera(door.camera, door.serial_number)

    @property
    def camera(self) -> MyQCamera | None:
        """Return the current camera metadata from discovery."""
        return self.door.camera if self.door is not None else None

    @property
    def available(self) -> bool:
        camera = self.camera
        return super().available and camera is not None and camera.online is not False


class MyQStandaloneCamera(CoordinatorEntity[MyQDataUpdateCoordinator], _MyQCameraMixin):
    """A camera attached to no door, such as the Video Keypad, on its own device."""

    _attr_has_entity_name = True
    _attr_translation_key = "camera"

    def __init__(self, coordinator: MyQDataUpdateCoordinator, camera: MyQCamera) -> None:
        super().__init__(coordinator)
        self._camera_serial = camera.serial_number
        self._attr_unique_id = f"{camera.serial_number}_camera"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, camera.serial_number)},
            manufacturer=MANUFACTURER,
            model=camera.device_model,
            name="Video Keypad" if camera.device_model == VIDEO_KEYPAD_MODEL else "Camera",
            serial_number=camera.serial_number,
        )
        # No opener serial: the stream picks the account's unattached camera.
        self._init_camera(camera, "")

    @property
    def camera(self) -> MyQCamera | None:
        return self.coordinator.cameras.get(self._camera_serial)

    @property
    def available(self) -> bool:
        camera = self.camera
        return super().available and camera is not None and camera.online is not False
