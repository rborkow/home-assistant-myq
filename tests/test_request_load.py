"""Request counts for account discovery and fresh device status."""

import asyncio
import json
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientSession

from custom_components.myq.client import MyQClient
from custom_components.myq.const import ACCOUNT_DISCOVERY_INTERVAL, DEFAULT_UPDATE_INTERVAL
from custom_components.myq.exceptions import MyQApiError
from custom_components.myq.models import MyQAccount
from custom_components.myq.oauth import MyQAuth

from .http import FakeResponse, FakeSession


def _device_response(state: str) -> FakeResponse:
    return FakeResponse(
        "",
        body=json.dumps(
            {
                "items": [
                    {
                        "device_family": "garagedoor",
                        "serial_number": "door-1",
                        "state": {"door_state": state},
                    }
                ]
            }
        ),
    )


async def test_account_cache_keeps_every_device_poll_fresh() -> None:
    session = FakeSession(
        request_responses=[
            FakeResponse("", body='{"accounts":[{"id":"account-1"}]}'),
            _device_response("closed"),
            _device_response("opening"),
            _device_response("open"),
        ]
    )
    auth = MagicMock(spec=MyQAuth)
    auth.async_access_token = AsyncMock(return_value="access")
    client = MyQClient(cast(ClientSession, session), auth)

    with patch("custom_components.myq.client.monotonic", return_value=0) as clock:
        for poll, expected in enumerate(("closed", "opening", "open")):
            clock.return_value = poll * DEFAULT_UPDATE_INTERVAL.total_seconds()
            devices = await client.async_get_devices()
            assert devices.doors[0].door_state == expected

    assert len(session.calls) == 4
    assert sum(call.url.endswith("/accounts") for call in session.calls) == 1


async def test_account_membership_is_refreshed_when_cache_expires() -> None:
    session = FakeSession(
        request_responses=[
            FakeResponse("", body='{"accounts":[{"id":"account-1"}]}'),
            FakeResponse("", body='{"accounts":[{"id":"account-2"}]}'),
        ]
    )
    auth = MagicMock(spec=MyQAuth)
    auth.async_access_token = AsyncMock(return_value="access")
    client = MyQClient(cast(ClientSession, session), auth)

    with patch("custom_components.myq.client.monotonic", return_value=0) as clock:
        assert (await client.async_get_accounts())[0].account_id == "account-1"
        clock.return_value = ACCOUNT_DISCOVERY_INTERVAL.total_seconds() - 1
        assert (await client.async_get_accounts())[0].account_id == "account-1"
        clock.return_value += 1
        assert (await client.async_get_accounts())[0].account_id == "account-2"

    assert len(session.calls) == 2


async def test_simultaneous_discovery_shares_the_account_request() -> None:
    client = MyQClient(MagicMock(), MagicMock())
    release = asyncio.Event()

    async def fetch_accounts() -> tuple[MyQAccount, ...]:
        await release.wait()
        return (MyQAccount("account-1", "Home"),)

    with patch.object(client, "_async_fetch_accounts", side_effect=fetch_accounts) as fetch:
        requests = [asyncio.create_task(client.async_get_accounts()) for _ in range(3)]
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.gather(*requests) == [(MyQAccount("account-1", "Home"),)] * 3
        fetch.assert_awaited_once()


async def test_failed_account_refresh_does_not_extend_the_cache() -> None:
    session = FakeSession(
        request_responses=[
            FakeResponse("", body='{"accounts":[{"id":"account-1"}]}'),
            FakeResponse("", body='{"accounts":[{"id":"account-2"},{}]}'),
            FakeResponse("", body='{"accounts":[{"id":"account-3"}]}'),
        ]
    )
    auth = MagicMock(spec=MyQAuth)
    auth.async_access_token = AsyncMock(return_value="access")
    client = MyQClient(cast(ClientSession, session), auth)

    with patch("custom_components.myq.client.monotonic", return_value=0) as clock:
        await client.async_get_accounts()
        clock.return_value = ACCOUNT_DISCOVERY_INTERVAL.total_seconds()
        with pytest.raises(MyQApiError, match="without an ID"):
            await client.async_get_accounts()
        assert (await client.async_get_accounts())[0].account_id == "account-3"

    assert len(session.calls) == 3


@pytest.mark.parametrize("account_count", [1, 2])
async def test_idle_request_budget_over_one_day(account_count: int) -> None:
    session = MagicMock(spec=ClientSession)

    def respond(method: str, url: str, *, headers: dict[str, str]) -> FakeResponse:
        del method, headers
        if url.endswith("/accounts"):
            return FakeResponse(
                "", body=json.dumps({"accounts": [{"id": str(i)} for i in range(account_count)]})
            )
        return FakeResponse("", body='{"items":[]}')

    session.request.side_effect = respond
    auth = MagicMock(spec=MyQAuth)
    auth.async_access_token = AsyncMock(return_value="access")
    client = MyQClient(session, auth)
    with patch("custom_components.myq.client.monotonic", return_value=0) as clock:
        for elapsed in range(0, 86400, int(DEFAULT_UPDATE_INTERVAL.total_seconds())):
            clock.return_value = elapsed
            await client.async_get_devices()

    account_requests = sum(
        call.args[1].endswith("/accounts") for call in session.request.call_args_list
    )
    assert account_requests == 90
    assert session.request.call_count == 90 + 720 * account_count
