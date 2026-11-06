"""Wake only for explicit keypad consumers and bound power polling and cancellation."""

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientSession

from custom_components.myq.client import MyQClient
from custom_components.myq.exceptions import MyQApiError
from custom_components.myq.keypad_wake import async_wake_keypad
from custom_components.myq.models import MyQCamera
from custom_components.myq.oauth import MyQAuth

from .http import FakeResponse, FakeSession

KEYPAD = MyQCamera("TC-0007-20000001", True, "vkp-camera")
FP = b'{"status":{"pwr":{"sleepMode":"FP"}}}'
LPDS = b'{"status":{"pwr":{"sleepMode":"LPDS"}}}'


async def test_wake_uses_owning_client_auth_headers_and_full_power_wait() -> None:
    session = FakeSession(
        request_responses=[
            FakeResponse("", body=b""),
            FakeResponse("", body=LPDS),
            FakeResponse("", body=FP),
        ]
    )
    auth = MagicMock(spec=MyQAuth)
    auth.async_access_token = AsyncMock(side_effect=["first", "renewed", "renewed"])
    client = MyQClient(cast(ClientSession, session), auth)
    with patch("custom_components.myq.keypad_wake._POLL_INTERVAL", 0):
        await client.async_prepare_keypad(KEYPAD)
    assert [call.method for call in session.calls] == ["POST", "GET", "GET"]
    assert "/devices/20000001/do/wakeup?timestamp=" in session.calls[0].url
    assert session.calls[0].kwargs["json"] == {}
    assert [call.kwargs["headers"]["Authorization"] for call in session.calls] == [
        "Bearer first",
        "Bearer renewed",
        "Bearer renewed",
    ]
    for call in session.calls:
        assert call.kwargs["allow_redirects"] is False
        assert {
            "App-Version",
            "BrandId",
            "User-Agent",
            "AntiCSRF",
            "SeedonkLocale",
            "partnerId",
        } <= call.kwargs["headers"].keys()
    assert auth.async_access_token.await_count == 3


async def test_ordinary_camera_never_wakes_or_requests_authorization() -> None:
    session = FakeSession()
    auth = MagicMock(spec=MyQAuth)
    auth.async_access_token = AsyncMock()
    client = MyQClient(cast(ClientSession, session), auth)
    await client.async_prepare_keypad(MyQCamera("TC-0005-10000001", True))
    assert session.calls == []
    auth.async_access_token.assert_not_awaited()


async def test_timeout_bounds_entire_wake_including_initial_http_request() -> None:
    async def blocked(*args: object) -> bytes:
        await asyncio.Event().wait()
        return b""

    request = AsyncMock(side_effect=blocked)
    with (
        patch("custom_components.myq.keypad_wake._WAKE_TIMEOUT", 0.01),
        pytest.raises(MyQApiError, match="full power"),
    ):
        await async_wake_keypad("20000001", request)
    request.assert_awaited_once()


async def test_cancel_stops_power_polling_without_background_task() -> None:
    polling = asyncio.Event()
    exited = asyncio.Event()

    async def request(method: str, url: str, body: dict[str, object] | None) -> bytes:
        del url, body
        if method == "POST":
            return b""
        polling.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()
        return FP

    task = asyncio.create_task(async_wake_keypad("20000001", request))
    await polling.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert exited.is_set()


@pytest.mark.parametrize("body", [b"not-json-private-response", b"[]"])
async def test_malformed_power_response_is_sanitized(body: bytes) -> None:
    request = AsyncMock(side_effect=[b"", body])
    with pytest.raises(MyQApiError, match="invalid power response") as error:
        await async_wake_keypad("20000001", request)
    assert "private-response" not in str(error.value)


async def test_invalid_identity_and_http_redirect_never_continue_wake() -> None:
    request = AsyncMock()
    with pytest.raises(MyQApiError, match="identifier"):
        await async_wake_keypad("../other", request)
    request.assert_not_awaited()
    session = FakeSession(request_responses=[FakeResponse("", status=302)])
    auth = MagicMock(spec=MyQAuth)
    auth.async_access_token = AsyncMock(return_value="access")
    client = MyQClient(cast(ClientSession, session), auth)
    with pytest.raises(MyQApiError, match="HTTP 302"):
        await client.async_prepare_keypad(KEYPAD)
    assert len(session.calls) == 1


async def test_wake_bounds_response_while_reading() -> None:
    session = FakeSession(request_responses=[FakeResponse("", body=b"x" * 1_024_001)])
    auth = MagicMock(spec=MyQAuth)
    auth.async_access_token = AsyncMock(return_value="access")
    client = MyQClient(cast(ClientSession, session), auth)
    with pytest.raises(MyQApiError, match="size limit"):
        await client.async_prepare_keypad(KEYPAD)
    assert len(session.calls) == 1


async def test_cancelled_wake_http_read_releases_response_context() -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    class BlockingResponse(FakeResponse):
        async def read(self) -> bytes:
            started.set()
            await asyncio.Event().wait()
            return b""

        async def __aexit__(self, *args: object) -> None:
            closed.set()

    session = FakeSession(request_responses=[BlockingResponse("")])
    auth = MagicMock(spec=MyQAuth)
    auth.async_access_token = AsyncMock(return_value="access")
    client = MyQClient(cast(ClientSession, session), auth)
    task = asyncio.create_task(client.async_prepare_keypad(KEYPAD))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert len(session.calls) == 1
