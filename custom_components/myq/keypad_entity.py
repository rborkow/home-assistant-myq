"""Shared identity for standalone keypad controls and local diagnostic entities."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import MyQDataUpdateCoordinator
from .keypad_state import KeypadState
from .models import MyQCamera


class MyQKeypadEntity(CoordinatorEntity[MyQDataUpdateCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: MyQDataUpdateCoordinator, keypad: MyQCamera, key: str) -> None:
        super().__init__(coordinator)
        self._serial = keypad.serial_number
        self._attr_unique_id = f"{self._serial}_keypad_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name="Video Keypad",
            manufacturer=MANUFACTURER,
            model="Video Keypad",
            serial_number=self._serial,
        )

    @property
    def keypad_state(self) -> KeypadState:
        return self.coordinator.keypad_state(self._serial)

    @property
    def available(self) -> bool:
        return super().available and any(
            k.serial_number == self._serial for k in self.coordinator.keypads
        )

    async def async_update(self) -> None:
        """Explicit entity updates still read cache; use the refresh-power action for I/O."""
