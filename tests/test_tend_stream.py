"""Exercise the local HTTP stream with real loopback connections."""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit

import pytest
from aiohttp import ClientSession

from custom_components.myq.tend_protocol import TendStreamError
from custom_components.myq.tend_stream import TendLiveStream, _Viewer

SPS = b"\x00\x00\x00\x01\x67s"
PPS = b"\x00\x00\x00\x01\x68p"
IDR = b"\x00\x00\x00\x01\x65i"
PFRAME = b"\x00\x00\x00\x01\x61p"


@pytest.fixture
def open_session() -> Generator[AsyncMock]:
    with patch(
        "custom_components.myq.tend_stream.TendSession.open", new_callable=AsyncMock
    ) as opened:
        opened.return_value = MagicMock(ended=False, close=AsyncMock())
        yield opened


@pytest.fixture
async def live(socket_enabled: None, open_session: AsyncMock) -> AsyncGenerator[TendLiveStream]:
    del socket_enabled, open_session
    stream = TendLiveStream("door-1", AsyncMock(return_value="token"))
    try:
        yield stream
    finally:
        await asyncio.wait_for(stream.close(), 2)


@pytest.mark.parametrize("error_type", [OSError, asyncio.CancelledError])
async def test_failed_server_start_releases_runner_and_can_retry(
    live: TendLiveStream, error_type: type[OSError] | type[asyncio.CancelledError]
) -> None:
    with patch(
        "custom_components.myq.tend_stream.web.TCPSite.start",
        autospec=True,
        side_effect=error_type,
    ) as start:
        with pytest.raises(error_type):
            await live.source_url()
        assert start.call_args.args[0]._runner.server is None
        assert live._runner is None
    assert (await live.source_url()).startswith("http://127.0.0.1:")


async def test_consumers_share_session_and_disconnect_independently(
    live: TendLiveStream, open_session: AsyncMock
) -> None:
    url = await live.source_url()
    async with ClientSession() as client:
        first = await client.get(url)
        second = await client.get(url)
        assert first.status == second.status == 200
        assert len(live._viewers) == 2
        open_session.assert_awaited_once()
        live._on_frame(PFRAME)
        live._on_frame(SPS)
        live._on_frame(PPS)
        live._on_frame(IDR)
        expected = SPS + PPS + IDR
        assert await asyncio.wait_for(first.content.readexactly(len(expected)), 1) == expected
        assert await asyncio.wait_for(second.content.readexactly(len(expected)), 1) == expected
        first.close()
        for _ in range(100):
            if len(live._viewers) == 1:
                break
            await asyncio.sleep(0.001)
        assert len(live._viewers) == 1
        live._on_frame(PFRAME)
        assert await asyncio.wait_for(second.content.readexactly(len(PFRAME)), 1) == PFRAME
        open_session.return_value.close.assert_not_awaited()
        second.close()


async def test_idle_session_is_closed(live: TendLiveStream, open_session: AsyncMock) -> None:
    with patch.object(live, "_idle_seconds", 0.01):
        async with ClientSession() as client:
            response = await client.get(await live.source_url())
            response.close()
        for _ in range(100):
            if open_session.return_value.close.await_count:
                break
            await asyncio.sleep(0.005)
    open_session.return_value.close.assert_awaited_once()
    assert live._session is None


async def test_signaling_loss_allows_fresh_connection(
    live: TendLiveStream, open_session: AsyncMock
) -> None:
    async with ClientSession() as client:
        url = await live.source_url()
        response = await client.get(url)
        open_session.return_value.ended = True
        live._on_frame(b"")
        assert await asyncio.wait_for(response.read(), 1) == b""
        previous = open_session.return_value
        open_session.return_value = MagicMock(ended=False, close=AsyncMock())
        response = await client.get(url)
        assert response.status == 200
        assert open_session.await_count == 2
        previous.close.assert_awaited_once()
        response.close()


async def test_no_frames_discards_stalled_session(
    live: TendLiveStream, open_session: AsyncMock
) -> None:
    with patch("custom_components.myq.tend_stream._FRAME_TIMEOUT", 0.01):
        async with ClientSession() as client:
            response = await client.get(await live.source_url())
            assert await asyncio.wait_for(response.read(), 1) == b""
            previous = open_session.return_value
            open_session.return_value = MagicMock(ended=False, close=AsyncMock())
            response = await client.get(await live.source_url())
            assert response.status == 200
            assert open_session.await_count == 2
            previous.close.assert_awaited_once()
            response.close()


async def test_failed_setup_returns_http_error(
    live: TendLiveStream, open_session: AsyncMock
) -> None:
    open_session.side_effect = TendStreamError("Camera unavailable")
    async with ClientSession() as client:
        response = await client.get(await live.source_url())
        assert response.status == 503
    assert not live._viewers


async def test_unknown_path_never_connects_camera(
    live: TendLiveStream, open_session: AsyncMock
) -> None:
    url = urlsplit(await live.source_url())
    assert url.hostname == "127.0.0.1"
    async with ClientSession() as client:
        response = await client.get(f"http://{url.netloc}/unknown")
        assert response.status == 404
    open_session.assert_not_awaited()


async def test_close_releases_nonreading_consumer(
    live: TendLiveStream, open_session: AsyncMock
) -> None:
    url = urlsplit(await live.source_url())
    reader, writer = await asyncio.open_connection(url.hostname, url.port, limit=1024)
    try:
        writer.write(f"GET {url.path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode())
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1)
        live._on_frame(SPS)
        live._on_frame(PPS)
        for _ in range(60):
            live._on_frame(IDR + bytes(256 * 1024))
        await asyncio.sleep(0.1)
        await asyncio.wait_for(live.close(), 1)
        open_session.return_value.close.assert_awaited_once()
        assert not live._viewers
    finally:
        writer.transport.abort()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


async def test_close_cancels_hung_setup(live: TendLiveStream, open_session: AsyncMock) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hang(*args: object, **kwargs: object) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    open_session.side_effect = hang
    url = urlsplit(await live.source_url())
    reader, writer = await asyncio.open_connection(url.hostname, url.port)
    writer.write(f"GET {url.path} HTTP/1.0\r\n\r\n".encode())
    await writer.drain()
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(live.close(), 1)
    assert cancelled.is_set()
    await asyncio.wait_for(reader.read(), 1)
    writer.close()
    await writer.wait_closed()


async def test_cancelled_close_finishes_cleanup(
    live: TendLiveStream, open_session: AsyncMock
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def finish_close() -> None:
        started.set()
        await release.wait()

    open_session.return_value.close.side_effect = finish_close
    async with ClientSession() as client:
        await client.get(await live.source_url())
        task = asyncio.create_task(live.close())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await live.close()
    assert live._session is None
    open_session.return_value.close.assert_awaited_once()


def test_queue_end_survives_backpressure() -> None:
    viewer = _Viewer()
    for _ in range(viewer.frames.maxsize):
        viewer.put(b"frame")
    viewer.end()
    viewer.put(b"late")
    queued = [viewer.frames.get_nowait() for _ in range(viewer.frames.qsize())]
    assert queued[-1] == b""
    assert b"late" not in queued


async def test_lan_only_applies_to_every_new_session(open_session: AsyncMock) -> None:
    stream = TendLiveStream("door-1", AsyncMock(return_value="token"), lan_only=True)
    try:
        await stream._subscribe()
        assert open_session.await_args is not None
        assert open_session.await_args.kwargs == {"lan_only": True}
        open_session.return_value.ended = True
        await stream._subscribe()
        assert open_session.await_count == 2
        assert open_session.await_args.kwargs == {"lan_only": True}
    finally:
        await stream.close()


async def test_recording_lease_shares_and_releases_only_its_viewer(
    live: TendLiveStream,
    open_session: AsyncMock,
) -> None:
    async with live.frames() as first:
        async with live.frames() as second:
            open_session.assert_awaited_once()
            live._on_frame(SPS + PPS + IDR)
            assert await first.get() == await second.get() == SPS + PPS + IDR
        assert len(live._viewers) == 1
        live._on_frame(PFRAME)
        assert await first.get() == PFRAME
    assert not live._viewers


def test_slow_consumer_ends_instead_of_dropping_a_reference_frame() -> None:
    viewer = _Viewer()
    for _ in range(61):
        viewer.put(PFRAME)
    assert viewer.ended
    assert viewer.frames.get_nowait() == b""
    assert viewer.frames.empty()
