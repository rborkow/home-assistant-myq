"""Cloud cooldowns block repeated HTTP requests while keeping services independent."""

import asyncio
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientConnectionError, ClientSession
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq.client import MyQClient
from custom_components.myq.coordinator import MyQDataUpdateCoordinator
from custom_components.myq.exceptions import MyQRateLimitError
from custom_components.myq.models import MyQCamera, MyQDevices, OAuthTokens
from custom_components.myq.oauth import MyQAuth
from custom_components.myq.request_limits import RequestCooldown

from .http import FakeResponse, FakeSession

IMAGE = b"\xff\xd8jpeg"
CAMERA = MyQCamera("TC-0005-10000001", True)


@pytest.mark.parametrize("status", [429, 503])
async def test_cloud_pause_blocks_snapshots_but_not_account_discovery(status: int) -> None:
    session = FakeSession(
        request_responses=[
            FakeResponse("", status=status, body="busy", headers={"Retry-After": "120"}),
            FakeResponse("", body='{"accounts":[{"id":"account-1"}]}'),
            FakeResponse("", body=IMAGE),
        ]
    )
    auth = MagicMock(spec=MyQAuth)
    auth.async_access_token = AsyncMock(return_value="access")
    client = MyQClient(cast(ClientSession, session), auth)

    with patch("custom_components.myq.request_limits.monotonic", return_value=100) as clock:
        with pytest.raises(MyQRateLimitError) as limited:
            await client.async_get_camera_image(CAMERA)
        assert limited.value.retry_after == 120
        for _ in range(3):
            with pytest.raises(MyQRateLimitError):
                await client.async_get_camera_image(CAMERA)
        assert len(session.calls) == 1
        auth.async_access_token.assert_awaited_once()

        assert (await client.async_get_accounts())[0].account_id == "account-1"
        assert len(session.calls) == 2
        clock.return_value = 219
        with pytest.raises(MyQRateLimitError) as remaining:
            await client.async_get_camera_image(CAMERA)
        assert remaining.value.retry_after == 1

        clock.return_value = 220
        assert await client.async_get_camera_image(CAMERA) == IMAGE
        assert len(session.calls) == 3


@pytest.mark.parametrize("header", [None, "", "invalid", "nan", "inf", "-1", "0", "1"])
def test_missing_or_invalid_retry_header_still_pauses_requests(header: str | None) -> None:
    cooldown = RequestCooldown()
    headers = {} if header is None else {"Retry-After": header}
    with patch("custom_components.myq.request_limits.monotonic", return_value=100):
        with pytest.raises(MyQRateLimitError) as limited:
            cooldown.check_response("https://devices.myq-cloud.com/devices", 429, headers)
        assert limited.value.retry_after == 60


def test_http_date_and_overlapping_responses_preserve_the_longest_pause() -> None:
    cooldown = RequestCooldown()
    url = "https://devices.myq-cloud.com/devices"
    date = format_datetime(datetime.fromtimestamp(1300, UTC), usegmt=True)
    with (
        patch("custom_components.myq.request_limits.monotonic", return_value=100) as clock,
        patch("custom_components.myq.request_limits.time", return_value=1000),
    ):
        with pytest.raises(MyQRateLimitError) as limited:
            cooldown.check_response(url, 429, {"Retry-After": date})
        assert limited.value.retry_after == 300
        clock.return_value = 101
        with pytest.raises(MyQRateLimitError) as overlapping:
            cooldown.check_response(url, 429, {"Retry-After": "60"})
        assert overlapping.value.retry_after == 299


async def test_concurrent_expired_tokens_share_the_identity_service_pause() -> None:
    session = FakeSession(
        post_responses=[
            FakeResponse("", status=429, body="busy", headers={"Retry-After": "300"}),
            FakeResponse("", body='{"access_token":"new-access","expires_in":3600}'),
        ]
    )
    listener = MagicMock()
    auth = MyQAuth(cast(ClientSession, session), OAuthTokens("old", "refresh", 0), listener)

    with patch("custom_components.myq.request_limits.monotonic", return_value=100) as clock:
        outcomes = await asyncio.gather(
            *(auth.async_access_token() for _ in range(3)), return_exceptions=True
        )
        assert all(isinstance(outcome, MyQRateLimitError) for outcome in outcomes)
        assert len(session.calls) == 1
        listener.assert_not_called()

        clock.return_value = 400
        assert await auth.async_access_token() == "new-access"
        assert len(session.calls) == 2
        listener.assert_called_once()


async def test_poll_failures_back_off_and_success_resets_the_delay(hass: HomeAssistant) -> None:
    client = MagicMock()
    client.async_get_devices = AsyncMock(side_effect=ClientConnectionError)
    coordinator = MyQDataUpdateCoordinator(hass, MockConfigEntry(), client)
    try:
        for expected in (240, 480, 900, 900):
            with pytest.raises(UpdateFailed) as failure:
                await coordinator._async_update_data()
            assert failure.value.retry_after == expected

        client.async_get_devices.side_effect = None
        client.async_get_devices.return_value = MyQDevices()
        assert await coordinator._async_update_data() == {}

        client.async_get_devices.side_effect = ClientConnectionError
        with pytest.raises(UpdateFailed) as recovered:
            await coordinator._async_update_data()
        assert recovered.value.retry_after == 240

        client.async_get_devices.side_effect = MyQRateLimitError(3600)
        with pytest.raises(UpdateFailed) as server_delay:
            await coordinator._async_update_data()
        assert server_delay.value.retry_after == 3600
    finally:
        await coordinator.async_shutdown()
