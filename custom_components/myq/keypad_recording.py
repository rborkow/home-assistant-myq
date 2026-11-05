"""Bounded recording of received keypad video, validated before publication."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .tend_protocol import TendStreamError
from .tend_stream import TendLiveStream, _nals

_FPS = 20
_MIN_FREE = 1_000_000_000
_MAX_STORED = 2_000_000_000


@dataclass(frozen=True, slots=True)
class RecordingResult:
    path: str
    frames: int
    media_seconds: float
    wall_seconds: float


def _reserve(directory: Path, partial: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    stored = sum(path.stat().st_size for path in directory.glob("*.mp4"))
    if shutil.disk_usage(directory).free < _MIN_FREE or stored >= _MAX_STORED:
        raise TendStreamError("Keypad recording storage limit reached")
    fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)


async def _checked_process(*args: str) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(90):
            output, errors = await process.communicate()
        if process.returncode or errors:
            raise TendStreamError("Keypad recording validation failed")
        return output
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def record_keypad(
    stream: TendLiveStream,
    seconds: int,
    directory: Path,
    ffmpeg: str,
) -> RecordingResult:
    """Count received video frames, allowing bounded recovery across interruptions."""
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 5 <= seconds <= 300:
        raise ValueError("Recording duration must be 5 to 300 seconds")
    destination = directory / f"{uuid4().hex}.mp4"
    partial = destination.with_suffix(".partial.mp4")
    process = None
    frames = 0
    target = seconds * _FPS
    started = time.monotonic()
    try:
        reservation = asyncio.create_task(asyncio.to_thread(_reserve, directory, partial))
        try:
            await asyncio.shield(reservation)
        except asyncio.CancelledError:
            # Cancelling to_thread does not stop its worker. Wait before deleting
            # the file so the worker cannot recreate it after cleanup.
            await asyncio.gather(reservation, return_exceptions=True)
            raise
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-fflags",
            "+genpts",
            "-framerate",
            str(_FPS),
            "-f",
            "h264",
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            "copy",
            "-bsf:v",
            f"setts=pts=N/({_FPS}*TB):dts=N/({_FPS}*TB)",
            "-movflags",
            "+faststart",
            str(partial),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdin is not None
        async with asyncio.timeout(seconds * 2 + 120):
            while frames < target:
                try:
                    async with stream.frames() as queue:
                        while frames < target:
                            async with asyncio.timeout(30):
                                frame = await queue.get()
                            if not frame:
                                break
                            process.stdin.write(frame)
                            await process.stdin.drain()
                            if {kind for kind, _ in _nals(frame)} & {1, 5}:
                                frames += 1
                except TendStreamError, TimeoutError, OSError:
                    if process.returncode is not None:
                        raise TendStreamError("Keypad recorder exited") from None
                if frames < target:
                    await asyncio.sleep(5)
        process.stdin.close()
        async with asyncio.timeout(20):
            await process.wait()
        if process.returncode:
            raise TendStreamError("Keypad recorder failed")
        # No session lease remains during disk validation.
        ffprobe = str(Path(ffmpeg).with_name("ffprobe"))
        data = await _checked_process(
            ffprobe,
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,nb_read_frames:format=duration",
            "-of",
            "json",
            str(partial),
        )
        info = json.loads(data)
        video = info.get("streams", [{}])[0]
        duration = float(info.get("format", {}).get("duration", 0))
        decoded = int(video.get("nb_read_frames", 0))
        if video.get("codec_name") != "h264" or duration < seconds - 0.2 or decoded < target - 2:
            raise TendStreamError("Keypad recording is incomplete")
        await _checked_process(
            ffmpeg, "-v", "error", "-xerror", "-i", str(partial), "-f", "null", "-"
        )
        await asyncio.to_thread(os.replace, partial, destination)
        return RecordingResult(
            str(destination), decoded, duration, round(time.monotonic() - started, 3)
        )
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        await asyncio.to_thread(partial.unlink, missing_ok=True)
