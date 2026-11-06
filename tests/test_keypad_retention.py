"""Deletion boundaries: only enrolled, unchanged clips can expire."""

import time
from pathlib import Path
from unittest.mock import patch

from homeassistant.core import HomeAssistant

from custom_components.myq.keypad_retention import KeypadRecordings


async def test_retention_preserves_existing_recent_and_replaced_files(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    recordings = KeypadRecordings(hass, "retention-test")
    recordings.root = tmp_path
    existing, expired, recent, replaced = [tmp_path / (c * 32 + ".mp4") for c in "abcd"]
    for path in (existing, expired, recent, replaced):
        path.write_bytes(b"video")
    now = time.time()
    with patch("custom_components.myq.keypad_retention.time.time", return_value=now - 31 * 86400):
        await recordings.async_add(str(expired))
        await recordings.async_add(str(replaced))
    await recordings.async_add(str(recent))
    replaced.write_bytes(b"replacement video must survive")
    # Reload proves ownership is durable; directory enumeration never enrolls old clips.
    restored = KeypadRecordings(hass, "retention-test")
    restored.root = tmp_path
    await restored.async_load()
    await restored.async_cleanup(0)
    assert expired.exists()
    await restored.async_cleanup(30)
    assert not expired.exists()
    assert existing.exists() and recent.exists() and replaced.exists()


async def test_retention_cannot_follow_symlink_or_parent_path(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    recordings = KeypadRecordings(hass, "retention-path-test")
    recordings.root = tmp_path / "recordings"
    recordings.root.mkdir()
    external = tmp_path / "outside.mp4"
    external.write_bytes(b"preserve")
    link = recordings.root / ("a" * 32 + ".mp4")
    link.symlink_to(external)
    expired = {"created_at": time.time() - 31 * 86400, "identity": {}}
    recordings._clips = {link.name: expired, "../outside.mp4": expired}
    await recordings.async_cleanup(30)
    assert external.read_bytes() == b"preserve"
    assert link.is_symlink()


async def test_failed_enrollment_does_not_mutate_ownership_ledger(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    from unittest.mock import AsyncMock

    import pytest

    recordings = KeypadRecordings(hass, "retention-save-failure")
    recordings.root = tmp_path
    old, new = [tmp_path / (c * 32 + ".mp4") for c in "ab"]
    old.write_bytes(b"existing managed video")
    new.write_bytes(b"new video")
    await recordings.async_add(str(old))
    previous = dict(recordings._clips)
    with (
        patch.object(recordings._store, "async_save", new=AsyncMock(side_effect=OSError("full"))),
        pytest.raises(OSError),
    ):
        await recordings.async_add(str(new))
    assert recordings._clips == previous
    restored = KeypadRecordings(hass, "retention-save-failure")
    await restored.async_load()
    assert restored._clips == previous
