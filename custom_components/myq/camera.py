"""Snapshots and live video from attached and standalone MyQ cameras."""

import asyncio
import time

from aiohttp import ClientError
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CAMERA_LAN_ONLY, DOMAIN
from .coordinator import MyQDataUpdateCoordinator
from .entity import myq_device_info
from .exceptions import MyQApiError, MyQError
from .models import GarageDoor, MyQCamera
from .runtime import MyQConfigEntry
from .tend_protocol import TendCameraTarget
from .tend_stream import TendLiveStream

SNAPSHOT_FRAME_INTERVAL = 30.0
_CAMERA_ALIAS_TIMEOUT = 5


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyQConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Keep attached cameras on their doors and add standalone camera devices."""
    del hass
    coordinator = entry.runtime_data.coordinator
    entities: list[MyQCameraEntity] = []
    for door in coordinator.data.values():
        if door.camera is not None:
            entities.append(MyQCameraEntity(coordinator, door.camera, door=door))
    entities.extend(MyQCameraEntity(coordinator, camera) for camera in coordinator.cameras.values())
    async_add_entities(entities)


class MyQCameraEntity(CoordinatorEntity[MyQDataUpdateCoordinator], Camera):
    """One camera identified independently of its optional garage-door device."""

    _attr_has_entity_name = True
    _attr_frame_interval = SNAPSHOT_FRAME_INTERVAL
    _attr_translation_key = "camera"

    def __init__(
        self,
        coordinator: MyQDataUpdateCoordinator,
        camera: MyQCamera,
        *,
        door: GarageDoor | None = None,
    ) -> None:
        super().__init__(coordinator)
        Camera.__init__(self)
        self._camera_serial = camera.serial_number
        self._opener_serial: str | None = None
        if door is not None:
            self._opener_serial = door.serial_number
            self._attr_unique_id = f"{door.serial_number}_camera"
            self._attr_device_info = myq_device_info(
                door.serial_number, name=door.name, model=door.device_model
            )
        else:
            self._attr_unique_id = f"{camera.serial_number}_camera"
            self._attr_device_info = myq_device_info(
                camera.serial_number, name=camera.name, model=camera.device_model
            )
        self._capture_lock = asyncio.Lock()
        self._image: bytes | None = None
        self._captured_at = 0.0
        self._live_stream: TendLiveStream | None = None
        if camera.serial_prefix == "TC":
            self._attr_supported_features = CameraEntityFeature.STREAM
            self.stream_options = {"use_wallclock_as_timestamps": True}

    @property
    def camera(self) -> MyQCamera | None:
        """Return the current camera metadata from discovery."""
        if self._opener_serial is None:
            return self.coordinator.cameras.get(self._camera_serial)
        door = self.coordinator.data.get(self._opener_serial)
        return door.camera if door is not None else None

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
        if not super().available or camera is None or camera.online is False:
            return False
        if self._opener_serial is not None:
            door = self.coordinator.data.get(self._opener_serial)
            return door is not None and door.online is not False
        return True

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Capture one JPEG, sharing the result with concurrent callers."""
        del width, height
        camera = self.camera
        if camera is None:
            return None
        async with self._capture_lock:
            if (
                self._image is not None
                and time.monotonic() - self._captured_at < SNAPSHOT_FRAME_INTERVAL
            ):
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
                self._async_stream_target,
                self.coordinator.client.async_access_token,
                fallback_target_supplier=(
                    self._async_camera_target if self._opener_serial is not None else None
                ),
                lan_only=bool(
                    self.coordinator.config_entry.options.get(CONF_CAMERA_LAN_ONLY, False)
                )
                if self.coordinator.config_entry
                else False,
            )
        return await self._live_stream.source_url()

    async def _async_stream_target(self) -> TendCameraTarget:
        if self._opener_serial is not None:
            return TendCameraTarget(opener_serial=self._opener_serial)
        return await self._async_camera_target()

    async def _async_camera_target(self) -> TendCameraTarget:
        """Resolve the current camera's exact destination within a bounded wait."""
        camera = self.camera
        if camera is None:
            raise MyQApiError("Camera is no longer available")
        async with asyncio.timeout(_CAMERA_ALIAS_TIMEOUT):
            destination = await self.coordinator.client.async_get_camera_alias(camera)
        return TendCameraTarget(destination=destination)

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
