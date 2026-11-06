"""Snapshots and live video from cameras attached to MyQ garage doors."""

import asyncio
import time

import voluptuous as vol
from aiohttp import ClientError
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import MyQDataUpdateCoordinator
from .entity import MyQEntity
from .exceptions import MyQError
from .keypad_camera import MyQKeypadCamera
from .models import GarageDoor, MyQCamera
from .runtime import MyQConfigEntry
from .tend_stream import TendLiveStream

SNAPSHOT_FRAME_INTERVAL = 5.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyQConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add cameras to their garage door devices."""
    del hass
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        MyQGarageCamera(coordinator, door)
        for door in coordinator.data.values()
        if door.camera is not None
    )
    async_add_entities(
        MyQKeypadCamera(coordinator, keypad) for keypad in coordinator.client.keypads
    )
    entity_platform.async_get_current_platform().async_register_entity_service(
        "record_keypad",
        {vol.Optional("duration"): vol.All(vol.Coerce(int), vol.Range(min=5, max=300))},
        "async_record_keypad",
    )
    entity_platform.async_get_current_platform().async_register_entity_service(
        "cancel_keypad_recording",
        {},
        "async_cancel_keypad_recording",
    )


class MyQGarageCamera(MyQEntity, Camera):
    """An attached camera using the account's existing authorization."""

    _attr_frame_interval = SNAPSHOT_FRAME_INTERVAL
    _attr_translation_key = "camera"

    async def async_record_keypad(self, duration: int = 30) -> None:
        """Reject the keypad-only service clearly when targeting another camera."""
        raise HomeAssistantError("Select a Video Keypad camera for this recording action")

    async def async_cancel_keypad_recording(self) -> None:
        raise HomeAssistantError("Select a Video Keypad camera for this recording action")

    def __init__(self, coordinator: MyQDataUpdateCoordinator, door: GarageDoor) -> None:
        super().__init__(coordinator, door)
        Camera.__init__(self)
        self._attr_unique_id = f"{door.serial_number}_camera"
        self._capture_lock = asyncio.Lock()
        self._image: bytes | None = None
        self._captured_at = 0.0
        self._live_stream: TendLiveStream | None = None
        if door.camera is not None and door.camera.serial_prefix == "TC":
            self._attr_supported_features = CameraEntityFeature.STREAM
            self.stream_options = {"use_wallclock_as_timestamps": True}

    @property
    def camera(self) -> MyQCamera | None:
        """Return the current camera metadata from discovery."""
        return self.door.camera if self.door is not None else None

    @property
    def model(self) -> str | None:
        return self.camera.device_model if self.camera is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        camera = self.camera
        if camera is None or camera.serial_prefix is None:
            return None
        return {"camera_serial_prefix": camera.serial_prefix}

    @property
    def available(self) -> bool:
        camera = self.camera
        return super().available and camera is not None and camera.online is not False

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Capture one JPEG, sharing the result with concurrent callers."""
        del width, height
        camera = self.camera
        if camera is None:
            return None
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
            self._live_stream = TendLiveStream(
                self._serial_number, self.coordinator.client.async_access_token
            )
        return await self._live_stream.source_url()

    async def async_will_remove_from_hass(self) -> None:
        """Close the source before waiting for the stream reader to stop."""
        try:
            if self._live_stream is not None:
                await self._live_stream.close()
        finally:
            self._live_stream = None
            if self.stream is not None:
                await self.stream.stop()
                self.stream = None
            await super().async_will_remove_from_hass()
