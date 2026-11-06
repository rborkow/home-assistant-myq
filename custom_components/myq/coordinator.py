import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic
from typing import Literal

from aiohttp import ClientError
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import MyQClient
from .const import (
    COMMAND_UPDATE_INITIAL_INTERVAL,
    COMMAND_UPDATE_INTERVAL,
    COMMAND_UPDATE_MIN_DURATION,
    COMMAND_UPDATE_TIMEOUT,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)
from .exceptions import MyQApiError, MyQAuthenticationError
from .keypad_retention import KeypadRecordings
from .keypad_state import KeypadPower, KeypadState
from .models import MyQCamera
from .runtime import MyQConfigEntry, MyQCoordinatorData

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _PendingCommand:
    target_state: Literal["open", "closed"]
    started_at: float
    motion: Literal["opening", "closing"] | None = None
    interval: timedelta = COMMAND_UPDATE_INITIAL_INTERVAL


class MyQDataUpdateCoordinator(DataUpdateCoordinator[MyQCoordinatorData]):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: MyQConfigEntry,
        client: MyQClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=DEFAULT_UPDATE_INTERVAL,
        )
        self.client = client
        self.recordings = KeypadRecordings(hass, entry.entry_id)
        self.keypad_states: dict[str, KeypadState] = {}
        self._keypad_state_loaded = False
        self._keypad_credential_renewals = 0
        self._power_locks: dict[str, asyncio.Lock] = {}
        self._keypad_store = Store[dict[str, object]](hass, 1, f"{DOMAIN}.{entry.entry_id}.keypads")
        self._pending_commands: dict[str, _PendingCommand] = {}
        self.cameras: dict[str, MyQCamera] = {}

    @property
    def keypads(self) -> tuple[MyQCamera, ...]:
        return tuple(c for c in self.cameras.values() if c.device_model == "vkp-camera")

    async def async_save_keypad_state(self) -> None:
        await self._keypad_store.async_save(self._keypad_storage_data())

    def keypad_state(self, serial: str) -> KeypadState:
        """Return local cached state; this method never requests cloud data."""
        return self.keypad_states.setdefault(
            serial, KeypadState(credential_renewals=self._keypad_credential_renewals)
        )

    async def async_load_keypad_state(self) -> None:
        data = await self._keypad_store.async_load() or {}
        for keypad in self.keypads:
            stored = data.get(keypad.serial_number)
            state = self.keypad_state(keypad.serial_number)
            if isinstance(stored, dict):
                clip, at = stored.get("last_clip"), stored.get("last_clip_at")
                if isinstance(clip, str) and isinstance(at, (int, float)) and 0 < at <= time.time():
                    state.last_clip, state.last_clip_at = clip, float(at)
            if isinstance(stored, dict) and isinstance(power := stored.get("power"), dict):
                self.keypad_state(keypad.serial_number).power = KeypadPower.from_storage(power)
        self._keypad_state_loaded = True

    def _keypad_storage_data(self) -> dict[str, object]:
        return {
            serial: {
                "power": state.power.as_dict() if state.power else None,
                "last_clip": state.last_clip,
                "last_clip_at": state.last_clip_at,
            }
            for serial, state in self.keypad_states.items()
        }

    @callback
    def async_keypad_changed(self) -> None:
        if self._keypad_state_loaded:
            self._keypad_store.async_delay_save(self._keypad_storage_data, 2)
        self.async_update_listeners()

    @callback
    def async_keypad_token_refreshed(self) -> None:
        self._keypad_credential_renewals += 1
        for state in self.keypad_states.values():
            state.credential_renewals = self._keypad_credential_renewals
        self.async_keypad_changed()

    async def async_refresh_keypad_power(self, serial: str) -> None:
        """Explicit refresh only; concurrent/repeated requests share a minute of cache."""
        async with self._power_locks.setdefault(serial, asyncio.Lock()):
            state = self.keypad_state(serial)
            if state.power and 0 <= time.time() - state.power.sampled_at < 60:
                return
            keypad = next((k for k in self.keypads if k.serial_number == serial), None)
            if keypad is None:
                raise MyQApiError("Keypad is no longer present")
            state.power = await self.client.async_get_keypad_power(keypad)
            self.async_keypad_changed()

    @callback
    def async_schedule_command_updates(
        self, serial_number: str, target_state: Literal["open", "closed"]
    ) -> None:
        """Schedule status updates with increasing delays after an accepted command."""
        if self.config_entry and self.config_entry.pref_disable_polling:
            return
        motion: Literal["opening", "closing"] | None = None
        door = self.data.get(serial_number)
        if door is not None:
            match (door.door_state, target_state):
                case ("closed", "open"):
                    motion = "opening"
                case ("open", "closed"):
                    motion = "closing"
        self._pending_commands[serial_number] = _PendingCommand(target_state, monotonic(), motion)
        self._update_command_interval()
        self._schedule_refresh()
        self.async_update_listeners()

    def command_motion(self, serial_number: str) -> Literal["opening", "closing"] | None:
        """Return provisional movement until a status response supersedes it."""
        command = self._pending_commands.get(serial_number)
        if command is None:
            return None
        return command.motion

    async def _async_update_data(self) -> MyQCoordinatorData:
        pending_at_request = self._pending_commands.copy()
        try:
            doors = await self.client.async_get_garage_doors()
        except MyQAuthenticationError as error:
            self._stop_command_updates(pending_at_request)
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="invalid_auth",
            ) from error
        except (ClientError, MyQApiError, TimeoutError) as error:
            self._stop_command_updates(pending_at_request)
            raise UpdateFailed("Unable to update MyQ garage doors") from error
        self.cameras = {camera.serial_number: camera for camera in self.client.unattached_cameras}
        data = {door.serial_number: door for door in doors}
        now = monotonic()
        for serial_number, command in pending_at_request.items():
            if self._pending_commands.get(serial_number) is not command:
                continue
            elapsed = now - command.started_at
            door = data.get(serial_number)
            if door is not None and (
                command.interval != COMMAND_UPDATE_INITIAL_INTERVAL
                or (command.motion, door.door_state)
                not in (("opening", "closed"), ("closing", "open"))
            ):
                command.motion = None
            if (
                elapsed >= COMMAND_UPDATE_TIMEOUT.total_seconds()
                or door is None
                or door.online is False
                or (
                    elapsed >= COMMAND_UPDATE_MIN_DURATION.total_seconds()
                    and door.door_state == command.target_state
                )
            ):
                del self._pending_commands[serial_number]
            else:
                command.interval = min(
                    command.interval + COMMAND_UPDATE_INITIAL_INTERVAL,
                    COMMAND_UPDATE_INTERVAL,
                )
        self._update_command_interval()
        return data

    def _stop_command_updates(self, pending_at_request: dict[str, _PendingCommand]) -> None:
        self._pending_commands = {
            serial_number: command
            for serial_number, command in self._pending_commands.items()
            if command is not pending_at_request.get(serial_number)
        }
        self._update_command_interval()

    def _update_command_interval(self) -> None:
        self.update_interval = min(
            (command.interval for command in self._pending_commands.values()),
            default=DEFAULT_UPDATE_INTERVAL,
        )
