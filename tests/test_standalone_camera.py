"""Standalone cameras retain their own identity, names, and availability."""

from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.camera import async_get_image
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq.camera import MyQCameraEntity
from custom_components.myq.const import (
    CONF_EMAIL,
    CONF_MFA_METHOD,
    CONF_TOKENS,
    DOMAIN,
    MFA_METHOD_EMAIL,
)
from custom_components.myq.models import GarageDoor, MyQCamera, MyQDevices
from custom_components.myq.runtime import MyQRuntimeData
from custom_components.myq.tend_protocol import CameraTargetSupplier, TendCameraTarget

FIRST = MyQCamera("TC-0007-20000001", True, "vkp-camera", "Front entrance")
SECOND = MyQCamera("TC-0007-20000002", True, "vkp-camera", "Side entrance")
ATTACHED = MyQCamera("TC-0005-10000001", True, "Integrated camera")
DOOR = GarageDoor("account-1", "door-1", "Garage", "Wi-Fi GDO", "closed", True, camera=ATTACHED)
IMAGE = b"\xff\xd8\xff\xe0jpeg"


async def test_standalone_targets_are_resolved_independently() -> None:
    coordinator = MagicMock()
    coordinator.data = {}
    coordinator.cameras = {camera.serial_number: camera for camera in (FIRST, SECOND)}
    coordinator.client.async_get_camera_alias = AsyncMock(
        side_effect=["account:mac1", "account:mac2"]
    )
    first = MyQCameraEntity(coordinator, FIRST)
    second = MyQCameraEntity(coordinator, SECOND)

    assert await first._async_stream_target() == TendCameraTarget(destination="account:mac1")
    assert await second._async_stream_target() == TendCameraTarget(destination="account:mac2")
    assert coordinator.client.async_get_camera_alias.await_args_list[0].args == (FIRST,)
    assert coordinator.client.async_get_camera_alias.await_args_list[1].args == (SECOND,)
    assert first.unique_id != second.unique_id
    assert first.device_info is not None
    assert first.device_info["name"] == "Front entrance"
    assert first.available and second.available


async def test_unavailable_and_non_tc_cameras_do_not_start_video() -> None:
    camera = replace(FIRST, serial_number="GW-0007-20000001")
    coordinator = MagicMock()
    coordinator.data = {}
    coordinator.cameras = {camera.serial_number: camera}
    entity = MyQCameraEntity(coordinator, camera)
    assert entity.supported_features == 0
    assert await entity.stream_source() is None
    entity = MyQCameraEntity(coordinator, FIRST)
    assert entity.available is False
    assert await entity.stream_source() is None
    assert await entity.async_camera_image() is None
    coordinator.client.async_get_camera_alias.assert_not_called()


@pytest.mark.parametrize("with_door", [False, True])
async def test_setup_preserves_devices_and_updates_cameras_without_extra_polling(
    hass: HomeAssistant, with_door: bool
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="driver@example.com",
        data={
            CONF_EMAIL: "driver@example.com",
            CONF_MFA_METHOD: MFA_METHOD_EMAIL,
            CONF_TOKENS: {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_at": 9876543210.0,
            },
        },
    )
    entry.add_to_hass(hass)
    client = MagicMock()
    doors = (DOOR,) if with_door else ()
    client.async_get_devices = AsyncMock(
        return_value=MyQDevices(doors, (ATTACHED, FIRST, SECOND) if with_door else (FIRST, SECOND))
    )
    client.async_get_camera_image = AsyncMock(return_value=IMAGE)
    with patch("custom_components.myq.MyQClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    ids = []
    for camera in (FIRST, SECOND):
        entity_id = registry.async_get_entity_id("camera", DOMAIN, f"{camera.serial_number}_camera")
        assert entity_id is not None
        ids.append(entity_id)
        device = device_registry.async_get_device(identifiers={(DOMAIN, camera.serial_number)})
        assert device is not None and device.name == camera.name
        entity = registry.async_get(entity_id)
        assert entity is not None and entity.device_id == device.id
        state = hass.states.get(entity_id)
        assert state is not None and state.attributes["friendly_name"] == f"{camera.name} Camera"
        assert (await async_get_image(hass, entity_id)).content == IMAGE
    assert client.async_get_camera_image.await_args_list[0].args == (FIRST,)
    assert client.async_get_camera_image.await_args_list[1].args == (SECOND,)
    if with_door:
        attached = registry.async_get_entity_id("camera", DOMAIN, "door-1_camera")
        assert attached is not None
        device = device_registry.async_get_device(identifiers={(DOMAIN, "door-1")})
        assert device is not None
        camera_entry = registry.async_get(attached)
        assert camera_entry is not None and camera_entry.device_id == device.id
        assert (
            registry.async_get_entity_id("camera", DOMAIN, f"{ATTACHED.serial_number}_camera")
            is None
        )
    else:
        assert registry.async_get_entity_id("cover", DOMAIN, "door-1") is None
    runtime = cast(MyQRuntimeData, entry.runtime_data)
    client.async_get_devices.return_value = MyQDevices(
        doors, (replace(FIRST, online=False), SECOND)
    )
    await runtime.coordinator.async_refresh()
    await hass.async_block_till_done()
    first = hass.states.get(ids[0])
    second = hass.states.get(ids[1])
    assert first is not None and first.state == STATE_UNAVAILABLE
    assert second is not None and second.state == "idle"
    client.async_get_devices.assert_awaited()
    assert client.async_get_devices.await_count == 2
    client.async_get_devices.return_value = MyQDevices(doors, (SECOND,))
    await runtime.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert FIRST.serial_number not in runtime.coordinator.cameras
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_two_live_streams_keep_distinct_camera_targets(socket_enabled: None) -> None:
    import asyncio
    from collections.abc import Callable

    from aiohttp import ClientSession

    del socket_enabled
    coordinator = MagicMock()
    coordinator.config_entry = None
    coordinator.data = {}
    coordinator.cameras = {camera.serial_number: camera for camera in (FIRST, SECOND)}
    coordinator.client.async_get_camera_alias = AsyncMock(
        side_effect=["account:mac1", "account:mac2"]
    )
    coordinator.client.async_access_token = AsyncMock(return_value="token")
    callbacks: dict[str, Callable[[bytes], None]] = {}
    sessions: list[MagicMock] = []

    async def open_session(
        target: TendCameraTarget,
        token: str,
        on_frame: Callable[[bytes], None],
        *,
        lan_only: bool,
        fallback_target_supplier: CameraTargetSupplier | None,
    ) -> MagicMock:
        assert fallback_target_supplier is None
        assert not lan_only
        assert token == "token"
        assert target.destination is not None
        callbacks[target.destination] = on_frame
        session = MagicMock(ended=False, close=AsyncMock())
        sessions.append(session)
        return session

    entities = [MyQCameraEntity(coordinator, camera) for camera in (FIRST, SECOND)]
    with patch("custom_components.myq.tend_stream.TendSession.open", side_effect=open_session):
        try:
            urls = [await entity.stream_source() for entity in entities]
            assert urls[0] is not None and urls[1] is not None
            assert urls[0] != urls[1]
            async with ClientSession() as client:
                first = await client.get(urls[0])
                second = await client.get(urls[1])
                sps = b"\x00\x00\x01\x67s"
                pps = b"\x00\x00\x01\x68p"
                first_frame = b"\x00\x00\x01\x65first"
                second_frame = b"\x00\x00\x01\x65second"
                for alias, frame in (("account:mac1", first_frame), ("account:mac2", second_frame)):
                    for nal in (sps, pps, frame):
                        callbacks[alias](nal)
                expected = sps + pps + first_frame
                assert (
                    await asyncio.wait_for(first.content.readexactly(len(expected)), 1) == expected
                )
                expected = sps + pps + second_frame
                assert (
                    await asyncio.wait_for(second.content.readexactly(len(expected)), 1) == expected
                )
                first.close()
                second.close()
        finally:
            for entity in entities:
                await entity.async_will_remove_from_hass()
    assert len(sessions) == 2
    for session in sessions:
        session.close.assert_awaited_once()
