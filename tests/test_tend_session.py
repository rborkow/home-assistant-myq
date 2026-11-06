"""Failure and cancellation coverage for camera connection ownership."""

import asyncio
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.myq.tend_protocol import TendStreamError, VideoOffer
from custom_components.myq.tend_session import TendSession


@pytest.fixture
def connection() -> Generator[MagicMock]:
    with patch("custom_components.myq.tend_session.CxsConnection") as factory:
        connection = factory.return_value
        connection.login = AsyncMock()
        connection.command = AsyncMock()
        connection.aclose = AsyncMock()
        connection.properties = {
            "device-info-list": "alias:mac1,AES=0123456789abcdef+GSN=other+LIP=192.0.2.1\n"
            "alias:mac2,AES=0123456789abcdef+GSN=door-1+LIP=192.0.2.2"
        }
        yield connection


@pytest.fixture
def transport() -> Generator[MagicMock]:
    with (
        patch("custom_components.myq.tend_session.VideoTransport") as factory,
        patch(
            "custom_components.myq.tend_session.request_video",
            AsyncMock(
                return_value=VideoOffer("relay.example", 30000, "0123456789abcdef", "LOG-test")
            ),
        ),
    ):
        factory.return_value.punch = AsyncMock()
        yield factory.return_value


async def test_session_selects_camera_and_closes(
    connection: MagicMock, transport: MagicMock
) -> None:
    session = await TendSession.open("door-1", "token", MagicMock())
    connection.command.assert_any_await("start-video-receive|alias|9|", "alias:mac2", "LOG-test")
    await session.close()
    transport.close.assert_called_once()
    connection.aclose.assert_awaited_once()


async def test_missing_camera_releases_signaling(connection: MagicMock) -> None:
    with pytest.raises(TendStreamError, match="not listed"):
        await TendSession.open("missing", "token", MagicMock())
    connection.aclose.assert_awaited_once()


async def test_failed_punch_is_released_before_retry(
    connection: MagicMock, transport: MagicMock
) -> None:
    transport.punch.side_effect = [TendStreamError("UDP failed"), None]
    with patch("custom_components.myq.tend_session._RETRY_DELAY", 0):
        session = await TendSession.open("door-1", "token", MagicMock())
    assert transport.punch.await_count == 2
    transport.close.assert_called_once()
    connection.command.assert_any_await("stop-video-receive|alias|9|", "alias:mac2", "LOG-test")
    await session.close()


async def test_cancelled_remote_stop_still_releases_resources(
    connection: MagicMock, transport: MagicMock
) -> None:
    session = await TendSession.open("door-1", "token", MagicMock())
    stopping = asyncio.Event()

    async def wait_for_stop(*args: object) -> None:
        stopping.set()
        await asyncio.Event().wait()

    connection.command.side_effect = wait_for_stop
    closing = asyncio.create_task(session.close())
    await stopping.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    transport.close.assert_called_once()
    connection.aclose.assert_awaited_once()
    assert session.ended


async def test_hung_remote_stop_is_bounded(connection: MagicMock, transport: MagicMock) -> None:
    session = await TendSession.open("door-1", "token", MagicMock())

    async def hang(*args: object) -> None:
        await asyncio.Event().wait()

    connection.command.side_effect = hang
    with patch("custom_components.myq.tend_session._STOP_TIMEOUT", 0.01):
        await asyncio.wait_for(session.close(), 1)
    transport.close.assert_called_once()
    connection.aclose.assert_awaited_once()


async def test_cancelled_setup_releases_offer(connection: MagicMock, transport: MagicMock) -> None:
    punching = asyncio.Event()

    async def hang(*args: object) -> None:
        punching.set()
        await asyncio.Event().wait()

    transport.punch.side_effect = hang
    opening = asyncio.create_task(TendSession.open("door-1", "token", MagicMock()))
    await punching.wait()
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    transport.close.assert_called_once()
    connection.command.assert_any_await("stop-video-receive|alias|9|", "alias:mac2", "LOG-test")
    connection.aclose.assert_awaited_once()


async def test_session_passes_lan_policy_to_transport(connection: MagicMock) -> None:
    with (
        patch("custom_components.myq.tend_session.VideoTransport") as factory,
        patch("custom_components.myq.tend_session.request_video", new_callable=AsyncMock) as offer,
    ):
        offer.return_value = VideoOffer("relay.example", 30000, "0123456789abcdef", "LOG-test")
        factory.return_value.punch = AsyncMock()
        session = await TendSession.open("door-1", "token", MagicMock(), lan_only=True)
        assert factory.call_args.kwargs == {"lan_only": True}
        await session.close()
