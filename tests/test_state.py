import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from psn_captures_backup.models import Capture
from psn_captures_backup.state import StateError, StateStore


def _capture(capture_id: str = "psn1") -> Capture:
    return Capture(
        id=capture_id,
        ugc_type=1,
        game_title="Game",
        upload_date=datetime(2025, 10, 11, tzinfo=UTC),
    )


def test_record_and_read_downloaded(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    store.record_downloaded(_capture(), Path("/out/Game/f.jpg"), "abc", 10)
    assert store.is_done("psn1") is True
    row = store.get("psn1")
    assert row is not None and row.local_path == "/out/Game/f.jpg"
    store.close()


def test_failed_then_downloaded_updates(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    store.record_failed(_capture(), "boom")
    assert store.is_done("psn1") is False
    store.record_downloaded(_capture(), Path("/out/f.jpg"), "abc", 10)
    assert store.is_done("psn1") is True
    store.close()


def test_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    store = StateStore(path)
    store.record_downloaded(_capture(), Path("/out/f.jpg"), "abc", 10)
    store.close()
    reopened = StateStore(path)
    assert reopened.is_done("psn1") is True
    reopened.close()


def test_unknown_id_is_none(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    assert store.get("nope") is None
    assert store.is_done("nope") is False
    store.close()


def test_parent_is_a_file_raises_state_error(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    with pytest.raises(StateError, match="state database"):
        StateStore(blocker / "state.sqlite")


def test_unwritable_parent_raises_state_error(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("permission checks are bypassed when running as root")
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o500)
    try:
        with pytest.raises(StateError, match="writable"):
            StateStore(readonly / "state.sqlite")
    finally:
        readonly.chmod(0o700)
