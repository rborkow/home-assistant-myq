"""Passive keypad previews, persistence, and cleanup without cloud requests."""

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.myq.camera import MyQStandaloneCamera
from custom_components.myq.keypad_preview import KeypadPreview
from custom_components.myq.models import MyQCamera
from custom_components.myq.tend_stream import TendLiveStream

SPS = b"\0\0\0\1\x67synthetic-sps"
PPS = b"\0\0\0\1\x68synthetic-pps"
IDR = b"\0\0\0\1\x65synthetic-idr"
PFRAME = b"\0\0\0\1\x61synthetic-pframe"
JPEG = b"\xff\xd8synthetic-jpeg"
KEYPAD = MyQCamera("TC-0007-20000001", True, "vkp-camera")


async def test_keypad_preview_requests_do_not_authorize_poll_or_start_live(
    hass: HomeAssistant,
) -> None:
    coordinator = MagicMock(hass=hass)
    coordinator.cameras = {KEYPAD.serial_number: KEYPAD}
    coordinator.client.async_get_camera_image = AsyncMock()
    coordinator.client.async_access_token = AsyncMock()
    camera = MyQStandaloneCamera(coordinator, KEYPAD)
    for _ in range(3):
        assert await camera.async_camera_image() is None
    assert camera._live_stream is None
    coordinator.client.async_get_camera_image.assert_not_awaited()
    coordinator.client.async_access_token.assert_not_awaited()


async def test_keyframe_cache_has_parameter_sets_and_does_not_subscribe() -> None:
    callback = MagicMock()
    token = AsyncMock()
    stream = TendLiveStream("", token, idle_seconds=20, on_snapshot=callback)
    assert not bool(stream.snapshot_frame)
    stream._on_frame(IDR)
    stream._on_frame(SPS)
    stream._on_frame(PPS)
    stream._on_frame(PFRAME)
    assert not bool(stream.snapshot_frame)
    stream._on_frame(IDR)
    assert stream.snapshot_frame == SPS + PPS + IDR
    callback.assert_called_once_with(SPS + PPS + IDR)
    stream._on_frame(PFRAME)
    stream._on_frame(b"")
    assert stream.snapshot_frame == SPS + PPS + IDR
    token.assert_not_awaited()
    await stream.close()


async def test_preview_survives_reload_and_shares_local_conversion(hass: HomeAssistant) -> None:
    first = KeypadPreview(hass, KEYPAD.serial_number)
    first.received(SPS + PPS + IDR)
    captured_at = first.captured_at
    await first.save()
    restored = KeypadPreview(hass, KEYPAD.serial_number)
    await restored.load()
    assert restored.captured_at == captured_at
    process = MagicMock(returncode=0, communicate=AsyncMock(return_value=(JPEG, b"")))
    with (
        patch("custom_components.myq.keypad_preview.async_setup_component", return_value=True),
        patch("custom_components.myq.keypad_preview.get_ffmpeg_manager") as manager,
        patch("asyncio.create_subprocess_exec", return_value=process) as spawn,
    ):
        manager.return_value.binary = "ffmpeg"
        results = await asyncio.gather(restored.image(), restored.image())
        assert tuple(results) == (JPEG, JPEG)
        spawn.assert_awaited_once()
        process.communicate.assert_awaited_once_with(SPS + PPS + IDR)
        assert await restored.image() == JPEG
        spawn.assert_awaited_once()


@pytest.mark.parametrize(
    "stored",
    [
        {"frame": "!", "at": 1.0},
        {"frame": base64.b64encode(IDR).decode(), "at": float("nan")},
        {"frame": base64.b64encode(IDR).decode(), "at": float("inf")},
        {"frame": base64.b64encode(IDR).decode(), "at": True},
        {"frame": "x" * 4_000_001, "at": 1.0},
    ],
)
async def test_bad_persisted_preview_does_not_start_decoder(
    hass: HomeAssistant, stored: dict[str, str | float]
) -> None:
    preview = KeypadPreview(hass, KEYPAD.serial_number)
    with patch.object(preview._store, "async_load", return_value=stored):
        await preview.load()
    with patch("asyncio.create_subprocess_exec") as spawn:
        assert await preview.image() is None
        spawn.assert_not_awaited()


async def test_cancelled_conversion_reaps_decoder(hass: HomeAssistant) -> None:
    preview = KeypadPreview(hass, KEYPAD.serial_number)
    preview.received(SPS + PPS + IDR)
    await preview.save()
    process = MagicMock(
        returncode=None, communicate=AsyncMock(side_effect=asyncio.CancelledError), wait=AsyncMock()
    )
    with (
        patch("custom_components.myq.keypad_preview.async_setup_component", return_value=True),
        patch("custom_components.myq.keypad_preview.get_ffmpeg_manager"),
        patch("asyncio.create_subprocess_exec", return_value=process),
        pytest.raises(asyncio.CancelledError),
    ):
        await preview.image()
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()
