import logging
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic
from typing import Literal

from aiohttp import ClientError
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import MyQClient
from .const import (
    COMMAND_UPDATE_INITIAL_INTERVAL,
    COMMAND_UPDATE_INTERVAL,
    COMMAND_UPDATE_MIN_DURATION,
    COMMAND_UPDATE_TIMEOUT,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    MAX_UPDATE_RETRY_INTERVAL,
)
from .exceptions import MyQApiError, MyQAuthenticationError, MyQRateLimitError
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
        self.cameras: dict[str, MyQCamera] = {}
        self._pending_commands: dict[str, _PendingCommand] = {}
        self._failure_retry_interval = DEFAULT_UPDATE_INTERVAL

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
            devices = await self.client.async_get_devices()
        except MyQAuthenticationError as error:
            self._stop_command_updates(pending_at_request)
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="invalid_auth",
            ) from error
        except (ClientError, MyQApiError, TimeoutError) as error:
            self._stop_command_updates(pending_at_request)
            self._failure_retry_interval = min(
                self._failure_retry_interval * 2, MAX_UPDATE_RETRY_INTERVAL
            )
            retry_after = self._failure_retry_interval.total_seconds()
            if isinstance(error, MyQRateLimitError):
                retry_after = max(retry_after, error.retry_after)
            raise UpdateFailed(
                "Unable to update MyQ garage doors", retry_after=retry_after
            ) from error
        self._failure_retry_interval = DEFAULT_UPDATE_INTERVAL
        self.cameras = {camera.serial_number: camera for camera in devices.standalone_cameras}
        data = {door.serial_number: door for door in devices.doors}
        now = monotonic()
        for serial_number, command in pending_at_request.items():
            if self._pending_commands.get(serial_number) is not command:
                continue
            elapsed = now - command.started_at
            door = data.get(serial_number)
            if door is not None and (
                (command.motion, door.door_state)
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
