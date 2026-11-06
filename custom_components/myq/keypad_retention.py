"""Retention applies only to files explicitly enrolled after successful recording."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)
_NAME = re.compile(r"[0-9a-f]{32}\.mp4")


def _identity(path: Path) -> dict[str, int]:
    stat = path.lstat()
    if path.is_symlink() or not path.is_file():
        raise ValueError("Recording must be a regular file")
    return {"inode": stat.st_ino, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _remove_owned(root: Path, name: str, identity: dict[str, object]) -> bool:
    """Never follow a path from storage or remove a replaced/symlinked file."""
    if not _NAME.fullmatch(name):
        return True
    path = root / name
    try:
        if _identity(path) == identity:
            path.unlink()
    except FileNotFoundError, ValueError:
        pass
    return True


class KeypadRecordings:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.root = (
            Path(hass.config.media_dirs.get("local", hass.config.path("media"))) / "myq-keypad"
        )
        self._store = Store[dict[str, dict[str, object]]](hass, 1, f"myq.{entry_id}.recordings")
        self._clips: dict[str, dict[str, object]] = {}
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        self._clips = await self._store.async_load() or {}

    async def async_add(self, path: str) -> None:
        candidate = Path(path)
        if candidate.parent != self.root or not _NAME.fullmatch(candidate.name):
            raise ValueError("Unexpected managed recording path")
        identity = await asyncio.to_thread(_identity, candidate)
        async with self._lock:
            updated = {
                **self._clips,
                candidate.name: {"created_at": time.time(), "identity": identity},
            }
            # A failed save must not enroll a failed publication in the in-memory ledger.
            await self._store.async_save(updated)
            self._clips = updated

    async def async_cleanup(self, days: int) -> None:
        if days <= 0:
            return
        async with self._lock:
            cutoff = time.time() - days * 86400
            changed = False
            for name, metadata in list(self._clips.items()):
                created = metadata.get("created_at")
                identity = metadata.get("identity")
                if not isinstance(created, (int, float)) or not isinstance(identity, dict):
                    continue
                if not 0 < created <= cutoff:
                    continue
                try:
                    await asyncio.to_thread(_remove_owned, self.root, name, identity)
                except OSError:
                    _LOGGER.warning("Could not remove an expired managed keypad recording")
                    continue
                del self._clips[name]
                changed = True
            if changed:
                await self._store.async_save(self._clips)
