"""Synthetic keypad inventory and media; never issue physical door commands."""

import asyncio
import json
import struct
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientSession

from custom_components.myq.client import MyQClient
from custom_components.myq.keypad_camera import MyQKeypadCamera
from custom_components.myq.keypad_protocol import relay_access_units
from custom_components.myq.keypad_session import keypad_destination
from custom_components.myq.models import MyQCamera, VideoKeypad
from custom_components.myq.oauth import MyQAuth
from custom_components.myq.tend_protocol import TendStreamError
from custom_components.myq.tend_stream import TendLiveStream

from .http import FakeResponse, FakeSession

CAMERA = MyQCamera("VK-0001-12345678", True, "vkp-camera", "aa:bb:cc:dd:ee:ff")
KEYPAD = VideoKeypad("account-1", "Entry keypad", CAMERA)


async def test_keypad_discovery_does_not_create_a_garage_door_and_removes_missing() -> None:
    inventory = {
        "items": [
            {
                "device_family": "camera",
                "device_type": "vkp-camera",
                "serial_number": CAMERA.serial_number,
                "name": KEYPAD.name,
                "device_model": "vkp-camera",
                "state": {"online": True, "mac_address": CAMERA.mac_address},
            }
        ]
    }
    session = FakeSession(
        request_responses=[
            FakeResponse("", body='{"accounts":[{"id":"account-1"}]}'),
            FakeResponse("", body=json.dumps(inventory)),
            FakeResponse("", body='{"accounts":[{"id":"account-1"}]}'),
            FakeResponse("", body='{"items":[]}'),
        ]
    )
    auth = MagicMock(spec=MyQAuth)
    auth.async_access_token = AsyncMock(return_value="synthetic-token")
    client = MyQClient(cast(ClientSession, session), auth)
    assert await client.async_get_garage_doors() == ()
    assert client.keypads == (KEYPAD,)
    assert await client.async_get_garage_doors() == ()
    assert not client.keypads


def test_selects_keypad_by_mac_and_rejects_other_camera() -> None:
    properties = {"device-info-list": "other:112233445566,MODEL=TC\nowner:AABBCCDDEEFF,MODEL=VK"}
    assert keypad_destination(properties, CAMERA) == "owner:AABBCCDDEEFF"
    with pytest.raises(TendStreamError):
        keypad_destination({"device-info-list": "other:112233445566,MODEL=TC"}, CAMERA)
    assert keypad_destination(properties, CAMERA, "owner:AABBCCDDEEFF") == "owner:AABBCCDDEEFF"
    with pytest.raises(TendStreamError):
        keypad_destination(properties, CAMERA, "unknown:112233445566")


def test_relay_rejects_wrong_camera_and_truncated_payload() -> None:
    alias = b"owner:AABBCCDDEEFF"
    frame = b"\x00\x00\x00\x01\x65test"
    body = b"\x03\x00\x00\x00" + struct.pack("<I", len(frame)) + frame
    packet = (
        b"\x05\x00" + struct.pack(">I", len(alias)) + alias + struct.pack(">I", len(body)) + body
    )
    assert relay_access_units(packet, alias.decode()) == [frame]
    with pytest.raises(ValueError):
        relay_access_units(packet, "different-camera")
    with pytest.raises(ValueError):
        relay_access_units(packet[:-1], alias.decode())


async def test_snapshot_without_live_frame_never_requests_token_or_cloud_image() -> None:
    coordinator = MagicMock()
    coordinator.client.keypads = (KEYPAD,)
    coordinator.client.async_access_token = AsyncMock()
    coordinator.client.async_get_camera_image = AsyncMock()
    entity = MyQKeypadCamera(coordinator, KEYPAD)
    assert await entity.async_camera_image() is None
    assert entity._live is None
    coordinator.client.async_access_token.assert_not_awaited()
    coordinator.client.async_get_camera_image.assert_not_awaited()


async def test_live_session_factory_is_lazy_and_snapshot_survives_idle() -> None:
    factory = AsyncMock(return_value=MagicMock(ended=False, close=AsyncMock()))
    token = AsyncMock(return_value="synthetic-token")
    stream = TendLiveStream("keypad", token, session_factory=factory, idle_seconds=20)
    try:
        token.assert_not_awaited()
        await stream._subscribe()
        factory.assert_awaited_once()
        keyframe = b"\x00\x00\x01\x67s\x00\x00\x01\x68p\x00\x00\x01\x65i"
        stream._on_frame(keyframe)
        assert stream.snapshot_frame == keyframe
        await stream._close_session()
        assert stream.snapshot_frame == keyframe
    finally:
        await stream.close()


async def test_viewer_and_recorder_share_session_until_last_lease_ends() -> None:
    closed = asyncio.Event()
    session = MagicMock(ended=False, close=AsyncMock(side_effect=closed.set))
    factory = AsyncMock(return_value=session)
    stream = TendLiveStream(
        "keypad",
        AsyncMock(return_value="synthetic-token"),
        session_factory=factory,
        idle_seconds=0,
    )
    try:
        async with stream.frames() as recording:
            viewer = await stream._subscribe()
            keyframe = b"\x00\x00\x01\x67s\x00\x00\x01\x68p\x00\x00\x01\x65i"
            stream._on_frame(keyframe)
            assert await recording.get() == keyframe
            assert await viewer.frames.get() == keyframe
            factory.assert_awaited_once()
            stream._unsubscribe(viewer)
            assert stream._idle_timer is None
            session.close.assert_not_awaited()
        await asyncio.wait_for(closed.wait(), 1)
        session.close.assert_awaited_once()
        assert stream.snapshot_frame == keyframe
    finally:
        await stream.close()


async def test_cancel_during_identity_lookup_does_not_login_or_wake() -> None:
    import threading
    from unittest.mock import patch

    from custom_components.myq.keypad_session import KeypadSession

    entered = asyncio.Event()
    finish = threading.Event()
    loop = asyncio.get_running_loop()
    session = KeypadSession(lambda _: None)

    def resolve(camera: MyQCamera, token: str) -> str:
        loop.call_soon_threadsafe(entered.set)
        assert finish.wait(2)
        return "synthetic:alias"

    with (
        patch.object(session, "_resolve_alias", side_effect=resolve),
        patch("custom_components.myq.keypad_session.login_routed") as login,
        patch.object(session, "_wake") as wake,
    ):
        session._worker = asyncio.create_task(asyncio.to_thread(session._run, CAMERA, "synthetic"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            session._stop.set()
        finally:
            finish.set()
        await session.close()
    assert session.ended
    login.assert_not_called()
    wake.assert_not_called()
