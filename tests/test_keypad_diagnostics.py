"""Power cache, stale data and explicit refresh without background camera calls."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq.coordinator import MyQDataUpdateCoordinator
from custom_components.myq.keypad_sensor import KEYPAD_SENSORS, MyQKeypadSensor
from custom_components.myq.keypad_state import KeypadPower, KeypadState
from custom_components.myq.models import MyQCamera, VideoKeypad

KEYPAD = VideoKeypad("synthetic", "Keypad", MyQCamera("VK-0001-12345678", True, "vkp-camera"))


@pytest.mark.parametrize("battery", [True, -1, 101, float("nan"), float("inf"), 10**300, "50"])
def test_bad_battery_values_are_unknown(battery: object) -> None:
    power = KeypadPower.from_device({"status": {"pwr": {"battLv": battery, "sleepMode": {}}}})
    assert power.battery_percent is None
    assert power.sleep_mode is None


def test_restore_preserves_sample_age_and_expires_without_refresh() -> None:
    with patch("custom_components.myq.keypad_state.time.time", return_value=100000):
        power = KeypadPower.from_storage(
            {"sampled_at": 100000 - 43201, "battery_percent": 65, "sleep_mode": "LPDS"}
        )
        assert power is not None and power.battery_percent == 65 and not power.fresh
        assert KeypadPower.from_storage({"sampled_at": 10**300}) is None


async def test_all_sensor_reads_and_explicit_updates_use_only_local_state() -> None:
    state = KeypadState(power=KeypadPower(time.time(), 65, "LPDS"))
    coordinator = MagicMock(last_update_success=True)
    coordinator.client.keypads = (KEYPAD,)
    coordinator.keypad_state.return_value = state
    coordinator.async_request_refresh = AsyncMock()
    coordinator.client.async_get_keypad_power = AsyncMock()
    coordinator.client.async_access_token = AsyncMock()
    for key in KEYPAD_SENSORS:
        sensor = MyQKeypadSensor(coordinator, KEYPAD, key)
        assert sensor.available
        _ = sensor.native_value
        await sensor.async_update()
    coordinator.async_request_refresh.assert_not_awaited()
    coordinator.client.async_get_keypad_power.assert_not_awaited()
    coordinator.client.async_access_token.assert_not_awaited()
    state.power = KeypadPower(time.time() - 43201, 65, "LPDS")
    assert not MyQKeypadSensor(coordinator, KEYPAD, "battery").available
    assert MyQKeypadSensor(coordinator, KEYPAD, "power_sample_time").available


async def test_explicit_power_requests_coalesce_and_persist_cache(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="myq", data={})
    entry.add_to_hass(hass)
    client = MagicMock(keypads=(KEYPAD,))
    client.async_get_keypad_power = AsyncMock(return_value=KeypadPower(time.time(), 71, "LPDS"))
    coordinator = MyQDataUpdateCoordinator(hass, entry, client)
    coordinator._keypad_store = MagicMock(async_load=AsyncMock(return_value=None))
    await coordinator.async_load_keypad_state()
    await asyncio.gather(
        *(coordinator.async_refresh_keypad_power(KEYPAD.camera.serial_number) for _ in range(3))
    )
    client.async_get_keypad_power.assert_awaited_once_with(KEYPAD.camera)
    saved = coordinator._keypad_storage_data()
    client.async_get_keypad_power.reset_mock()
    restored = MyQDataUpdateCoordinator(hass, entry, client)
    restored._keypad_store = MagicMock(async_load=AsyncMock(return_value=saved))
    await restored.async_load_keypad_state()
    assert (
        restored.keypad_state(KEYPAD.camera.serial_number).power
        == coordinator.keypad_state(KEYPAD.camera.serial_number).power
    )
    client.async_get_keypad_power.assert_not_awaited()


async def test_cooldown_rejects_trigger_before_any_cloud_work(hass: HomeAssistant) -> None:
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.myq.keypad_camera import MyQKeypadCamera

    coordinator = MagicMock()
    coordinator.hass = hass
    coordinator.config_entry.options = {"recording_cooldown": 60}
    camera = MyQKeypadCamera(coordinator, KEYPAD)
    camera._last_record_started = time.monotonic()
    with patch.object(camera, "stream_source", new_callable=AsyncMock) as source:
        with pytest.raises(HomeAssistantError, match="cooldown"):
            await camera.async_record_keypad(5)
        source.assert_not_awaited()


async def test_cancel_action_does_not_close_other_viewers(hass: HomeAssistant) -> None:
    from custom_components.myq.keypad_camera import MyQKeypadCamera

    coordinator = MagicMock()
    coordinator.hass = hass
    camera = MyQKeypadCamera(coordinator, KEYPAD)
    camera._live = MagicMock(close=AsyncMock())
    camera._record_task = asyncio.create_task(asyncio.sleep(60))
    await camera.async_cancel_keypad_recording()
    assert camera._record_task.cancelled()
    camera._live.close.assert_not_awaited()


async def test_snapshot_restore_publishes_timestamp_without_cloud(hass: HomeAssistant) -> None:
    from custom_components.myq.keypad_camera import MyQKeypadCamera

    state = KeypadState()
    coordinator = MagicMock()
    coordinator.hass = hass
    coordinator.keypad_state.return_value = state
    coordinator.client.async_access_token = AsyncMock()
    camera = MyQKeypadCamera(coordinator, KEYPAD)
    camera._snapshot_store = MagicMock(
        async_load=AsyncMock(return_value={"frame": "a2V5ZnJhbWU=", "at": 1000.0})
    )
    with patch(
        "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
        new_callable=AsyncMock,
    ):
        await camera.async_added_to_hass()
    assert camera._restored_frame == b"keyframe"
    assert state.snapshot_at == 1000.0
    coordinator.async_keypad_changed.assert_called_once()
    coordinator.client.async_access_token.assert_not_awaited()
