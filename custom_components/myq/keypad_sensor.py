"""Keypad diagnostics read only local cache; entity updates never wake or poll it."""

from datetime import UTC, datetime
from pathlib import Path

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.helpers.typing import StateType

from .coordinator import MyQDataUpdateCoordinator
from .keypad_entity import MyQKeypadEntity
from .keypad_transport import DEFAULT_POLICY
from .models import VideoKeypad

KEYPAD_SENSORS = {
    "battery": "Battery",
    "power_state": "Reported power state",
    "power_sample_time": "Battery sample time",
    "session_state": "Session state",
    "transport": "Video transport",
    "session_errors": "Session errors",
    "startup_time": "Last startup time",
    "active_consumers": "Active consumers",
    "credential_renewals": "Credential renewals",
    "recording_status": "Recording status",
    "last_clip": "Last recording",
    "snapshot_time": "Snapshot time",
}


class MyQKeypadSensor(MyQKeypadEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: MyQDataUpdateCoordinator, keypad: VideoKeypad, key: str
    ) -> None:
        super().__init__(coordinator, keypad, key)
        self.key = key
        self._attr_name = KEYPAD_SENSORS[key]
        if key == "battery":
            self._attr_device_class = SensorDeviceClass.BATTERY
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif key in {"power_sample_time", "last_clip", "snapshot_time"}:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        elif key == "startup_time":
            self._attr_device_class = SensorDeviceClass.DURATION
            self._attr_native_unit_of_measurement = UnitOfTime.SECONDS

    @property
    def available(self) -> bool:
        if self.key in {"battery", "power_state"}:
            power = self.keypad_state.power
            return super().available and power is not None and power.fresh
        return super().available

    @property
    def native_value(self) -> StateType | datetime:
        state = self.keypad_state
        power = state.power
        match self.key:
            case "battery":
                return power.battery_percent if power else None
            case "power_state":
                return power.sleep_mode if power else None
            case "power_sample_time":
                return datetime.fromtimestamp(power.sampled_at, UTC) if power else None
            case "transport":
                return state.transport
            case "session_state":
                return state.phase
            case "session_errors":
                return state.session_errors
            case "startup_time":
                return state.first_frame_seconds
            case "active_consumers":
                return state.active_consumers
            case "credential_renewals":
                return state.credential_renewals
            case "last_clip":
                return (
                    datetime.fromtimestamp(state.last_clip_at, UTC) if state.last_clip_at else None
                )
            case "snapshot_time":
                return datetime.fromtimestamp(state.snapshot_at, UTC) if state.snapshot_at else None
            case "recording_status":
                return state.recording_status
        return None

    @property
    def extra_state_attributes(self) -> dict[str, str | float | None] | None:
        if self.key == "transport":
            entry = self.coordinator.config_entry
            return {
                "policy": str(entry.options.get("transport_policy", DEFAULT_POLICY))
                if entry
                else DEFAULT_POLICY,
                "last_transport": self.keypad_state.last_transport,
                "cloud_fallback_used": self.keypad_state.fallback_used,
                "lan_keyframe_recoveries": self.keypad_state.lan_recoveries,
            }
        if self.key == "last_clip" and self.keypad_state.last_clip:
            return {
                "media_content_id": "media-source://media_source/local/myq-keypad/"
                + Path(self.keypad_state.last_clip).name,
                "media_content_type": "video/mp4",
            }
        if self.key == "session_errors":
            state = self.keypad_state
            return {"last_failure": state.last_failure, "failure_at": state.failure_at}
        return None
