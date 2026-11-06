"""Recording safety and cancellation; no real camera or door commands."""

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.myq.keypad_recording import _publish_recording, _reserve, record_keypad
from custom_components.myq.tend_protocol import TendStreamError
from custom_components.myq.tend_stream import TendLiveStream


def test_low_disk_space_refuses_recording_without_partial_file(tmp_path: Path) -> None:
    path = tmp_path / "test.partial.mp4"
    with patch("custom_components.myq.keypad_recording.shutil.disk_usage") as usage:
        usage.return_value.free = 10
        with pytest.raises(TendStreamError):
            _reserve(tmp_path, path)
    assert not path.exists()


@pytest.mark.parametrize("duration", [0, 4, 301, True])
async def test_invalid_duration_does_not_start_camera(tmp_path: Path, duration: int) -> None:
    stream = MagicMock(spec=TendLiveStream)
    with pytest.raises(ValueError):
        await record_keypad(stream, duration, tmp_path, "ffmpeg", enroll=AsyncMock())
    stream.frames.assert_not_called()
    assert not await asyncio.to_thread(lambda: list(tmp_path.iterdir()))


async def test_cancellation_during_reservation_waits_for_worker_then_cleans(
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    finish = threading.Event()
    loop = asyncio.get_running_loop()

    def reserve(directory: Path, partial: Path) -> None:
        loop.call_soon_threadsafe(started.set)
        assert finish.wait(2)
        partial.touch()

    stream = MagicMock(spec=TendLiveStream)
    with patch("custom_components.myq.keypad_recording._reserve", side_effect=reserve):
        task = asyncio.create_task(record_keypad(stream, 5, tmp_path, "ffmpeg", enroll=AsyncMock()))
        try:
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    stream.frames.assert_not_called()
    assert not await asyncio.to_thread(lambda: list(tmp_path.iterdir()))


async def test_cancelled_recording_releases_lease_kills_encoder_and_cleans_file(
    tmp_path: Path,
) -> None:
    leased = asyncio.Event()
    released = asyncio.Event()

    @asynccontextmanager
    async def frames() -> AsyncIterator[asyncio.Queue[bytes]]:
        leased.set()
        try:
            yield asyncio.Queue()
        finally:
            released.set()

    stream = MagicMock(spec=TendLiveStream)
    stream.frames.side_effect = frames
    encoder = MagicMock(returncode=None, wait=AsyncMock())
    encoder.kill.side_effect = lambda: setattr(encoder, "returncode", -9)
    with (
        patch(
            "custom_components.myq.keypad_recording.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=encoder),
        ),
        patch("custom_components.myq.keypad_recording.shutil.disk_usage") as usage,
    ):
        usage.return_value.free = 10_000_000_000
        task = asyncio.create_task(
            record_keypad(cast(TendLiveStream, stream), 5, tmp_path, "ffmpeg", enroll=AsyncMock())
        )
        await asyncio.wait_for(leased.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert released.is_set()
    encoder.kill.assert_called_once()
    encoder.wait.assert_awaited_once()
    assert not await asyncio.to_thread(lambda: list(tmp_path.iterdir()))


async def test_late_cancel_waits_for_rename_worker_and_enrollment(tmp_path: Path) -> None:
    partial, destination = tmp_path / "new.partial.mp4", tmp_path / "new.mp4"
    partial.write_bytes(b"validated video")
    started = asyncio.Event()
    finish = threading.Event()
    loop = asyncio.get_running_loop()
    enroll = AsyncMock()

    def rename(source: Path, target: Path) -> None:
        loop.call_soon_threadsafe(started.set)
        assert finish.wait(2)
        source.rename(target)

    with patch("custom_components.myq.keypad_recording.os.replace", side_effect=rename):
        task = asyncio.create_task(_publish_recording(partial, destination, enroll))
        try:
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            finish.set()
        await asyncio.wait_for(task, 2)
    assert not task.cancelled()
    assert destination.read_bytes() == b"validated video"
    assert not partial.exists()
    enroll.assert_awaited_once_with(str(destination))


async def test_late_cancel_during_enrollment_completes_transaction(tmp_path: Path) -> None:
    partial, destination = tmp_path / "new.partial.mp4", tmp_path / "new.mp4"
    partial.write_bytes(b"validated video")
    started, finish = asyncio.Event(), asyncio.Event()
    enrolled: list[str] = []

    async def enroll(path: str) -> None:
        started.set()
        await finish.wait()
        enrolled.append(path)

    task = asyncio.create_task(_publish_recording(partial, destination, enroll))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    await asyncio.wait_for(task, 2)
    assert enrolled == [str(destination)]
    assert destination.exists() and not task.cancelled()


async def test_failed_enrollment_rolls_back_only_new_clip(tmp_path: Path) -> None:
    existing = tmp_path / "existing.mp4"
    existing.write_bytes(b"preserve")
    partial, destination = tmp_path / "new.partial.mp4", tmp_path / "new.mp4"
    partial.write_bytes(b"validated video")
    with pytest.raises(OSError):
        await _publish_recording(partial, destination, AsyncMock(side_effect=OSError("disk full")))
    assert not destination.exists() and not partial.exists()
    assert existing.read_bytes() == b"preserve"


async def test_recording_reports_success_when_cancel_arrives_during_final_commit(
    tmp_path: Path,
) -> None:
    import json

    queue: asyncio.Queue[bytes] = asyncio.Queue()
    for _ in range(100):
        queue.put_nowait(b"\x00\x00\x00\x01\x65synthetic-idr")

    @asynccontextmanager
    async def frames() -> AsyncIterator[asyncio.Queue[bytes]]:
        yield queue

    stream = MagicMock(spec=TendLiveStream)
    stream.frames.side_effect = frames
    encoder = MagicMock(returncode=0, wait=AsyncMock())
    encoder.stdin.drain = AsyncMock()
    enrolling, finish = asyncio.Event(), asyncio.Event()
    enrolled: list[str] = []

    async def enroll(path: str) -> None:
        enrolling.set()
        await finish.wait()
        enrolled.append(path)

    probe = json.dumps(
        {"streams": [{"codec_name": "h264", "nb_read_frames": 100}], "format": {"duration": 5}}
    ).encode()
    with (
        patch(
            "custom_components.myq.keypad_recording.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=encoder),
        ),
        patch(
            "custom_components.myq.keypad_recording._checked_process",
            new=AsyncMock(side_effect=[probe, b""]),
        ),
        patch("custom_components.myq.keypad_recording.shutil.disk_usage") as usage,
    ):
        usage.return_value.free = 10_000_000_000
        task = asyncio.create_task(
            record_keypad(cast(TendLiveStream, stream), 5, tmp_path, "ffmpeg", enroll=enroll)
        )
        await asyncio.wait_for(enrolling.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        result = await asyncio.wait_for(task, 2)
    assert enrolled == [result.path]
    assert await asyncio.to_thread(Path(result.path).exists) and not task.cancelled()
    assert await asyncio.to_thread(lambda: list(tmp_path.glob("*.partial.mp4"))) == []
