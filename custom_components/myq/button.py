"""Explicit keypad controls; no background camera status polling."""

from aiohttp import ClientError
from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import MyQDataUpdateCoordinator
from .exceptions import MyQError
from .keypad_entity import MyQKeypadEntity
from .models import VideoKeypad
from .runtime import MyQConfigEntry

BUTTONS = {
    "refresh_power": "Refresh battery and power",
    "record": "Record clip",
    "cancel_recording": "Cancel recording",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyQConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    del hass
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        MyQKeypadButton(coordinator, keypad, key)
        for keypad in coordinator.client.keypads
        for key in BUTTONS
    )


class MyQKeypadButton(MyQKeypadEntity, ButtonEntity):
    def __init__(
        self, coordinator: MyQDataUpdateCoordinator, keypad: VideoKeypad, key: str
    ) -> None:
        super().__init__(coordinator, keypad, key)
        self.key = key
        self._attr_name = BUTTONS[key]

    async def async_press(self) -> None:
        if self.key == "refresh_power":
            try:
                await self.coordinator.async_refresh_keypad_power(self._serial)
            except (MyQError, ClientError, TimeoutError) as error:
                raise HomeAssistantError("Could not refresh keypad battery and power") from error
            return
        camera = er.async_get(self.hass).async_get_entity_id(
            "camera", DOMAIN, f"{self._serial}_keypad_camera"
        )
        if camera is None:
            raise HomeAssistantError("Keypad camera is not available")
        await self.hass.services.async_call(
            DOMAIN,
            "record_keypad" if self.key == "record" else "cancel_keypad_recording",
            {"entity_id": camera},
            blocking=True,
        )
