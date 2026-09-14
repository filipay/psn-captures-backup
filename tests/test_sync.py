from datetime import UTC, datetime
from pathlib import Path

from psn_captures_backup.config import Settings
from psn_captures_backup.models import Capture
from psn_captures_backup.state import StateStore
from psn_captures_backup.sync import sync


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = Settings(
        npsso="x",
        output_dir=tmp_path / "captures",
        state_file=tmp_path / "state.sqlite",
        token_file=tmp_path / "token.json",
    )
    return base.with_overrides(**overrides)


def _capture(capture_id: str, ugc_type: int = 1) -> Capture:
    return Capture(
        id=capture_id,
        ugc_type=ugc_type,
        game_title="Game",
        upload_date=datetime(2025, 10, 11, tzinfo=UTC),
        screenshot_url="https://cdn/x.jpg",
        download_url="https://cdn/x.mp4",
        file_type="JPEG" if ugc_type == 1 else "MP4",
    )


class FakeTokens:
    def get_access_token(self) -> str:
        return "TOKEN"

    def close(self) -> None:
        pass


class FakePsn:
    def __init__(self, captures):
        self._captures = captures
        self.downloaded: list[str] = []

    def list_captures(self):
        return self._captures

    def download_to(self, capture, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"data")
        self.downloaded.append(capture.id)
        return "deadbeef", 4

    def close(self) -> None:
        pass


def test_sync_downloads_new_captures(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state = StateStore(settings.state_file)
    result = sync(settings, tokens=FakeTokens(), psn=FakePsn([_capture("a"), _capture("b")]), state=state)
    assert result.listed == 2
    assert result.downloaded == 2
    assert (settings.output_dir / "Game" / "2025-10-11_a.jpg").exists()
    state.close()


def test_sync_skips_already_downloaded(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state = StateStore(settings.state_file)
    first = sync(settings, tokens=FakeTokens(), psn=FakePsn([_capture("a")]), state=state)
    assert first.downloaded == 1
    second = sync(settings, tokens=FakeTokens(), psn=FakePsn([_capture("a")]), state=state)
    assert second.skipped == 1
    assert second.downloaded == 0
    state.close()


def test_sync_redownloads_when_file_missing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state = StateStore(settings.state_file)
    sync(settings, tokens=FakeTokens(), psn=FakePsn([_capture("a")]), state=state)
    (settings.output_dir / "Game" / "2025-10-11_a.jpg").unlink()
    result = sync(settings, tokens=FakeTokens(), psn=FakePsn([_capture("a")]), state=state)
    assert result.downloaded == 1
    state.close()


def test_sync_respects_media_filters(tmp_path: Path) -> None:
    settings = _settings(tmp_path, include_videos=False)
    state = StateStore(settings.state_file)
    result = sync(
        settings,
        tokens=FakeTokens(),
        psn=FakePsn([_capture("img"), _capture("vid", ugc_type=2)]),
        state=state,
    )
    assert result.downloaded == 1
    state.close()


def test_sync_dry_run_downloads_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path, dry_run=True)
    state = StateStore(settings.state_file)
    result = sync(settings, tokens=FakeTokens(), psn=FakePsn([_capture("a")]), state=state)
    assert result.downloaded == 0
    assert result.skipped == 1
    assert not (settings.output_dir / "Game" / "2025-10-11_a.jpg").exists()
    state.close()


def test_sync_records_failures(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state = StateStore(settings.state_file)

    class FailingPsn(FakePsn):
        def download_to(self, capture, dest: Path):
            from psn_captures_backup.psn import PsnError

            raise PsnError("nope")

    result = sync(settings, tokens=FakeTokens(), psn=FailingPsn([_capture("a")]), state=state)
    assert result.failed == 1
    assert state.is_done("a") is False
    state.close()
