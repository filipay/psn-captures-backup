from pathlib import Path

import pytest

from psn_captures_backup import cli
from psn_captures_backup.auth import AuthError


def test_missing_npsso_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("NPSSO", raising=False)
    monkeypatch.setenv("PSN_OUTPUT_DIR", str(tmp_path))
    assert cli.main(["sync"]) == 2


def test_list_prints_captures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("NPSSO", "x")
    monkeypatch.setenv("PSN_OUTPUT_DIR", str(tmp_path))

    class FakeTokens:
        def get_access_token(self):
            return "TOKEN"

        def close(self):
            pass

    class FakePsn:
        def list_captures(self):
            from datetime import UTC, datetime

            from psn_captures_backup.models import Capture

            return [
                Capture(
                    id="psn1",
                    ugc_type=1,
                    game_title="Game",
                    upload_date=datetime(2025, 10, 11, tzinfo=UTC),
                    file_type="JPEG",
                )
            ]

        def close(self):
            pass

    monkeypatch.setattr(cli, "_build_tokens", lambda settings: FakeTokens())
    monkeypatch.setattr(cli, "_build_psn", lambda token: FakePsn())
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "psn1" in out
    assert "Game" in out


def test_auth_command_does_not_crash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    # Regression: the `auth` subparser defines no --output-dir/--state-file, so
    # _settings_for must not assume every namespace has them.
    monkeypatch.setenv("NPSSO", "x")
    monkeypatch.setenv("PSN_OUTPUT_DIR", str(tmp_path))

    class FakeTokens:
        def get_access_token(self):
            return "TOKEN"

        def close(self):
            pass

    monkeypatch.setattr(cli, "_build_tokens", lambda settings: FakeTokens())
    assert cli.main(["auth", "--check"]) == 0
    assert "Authentication OK" in capsys.readouterr().out


def test_sync_reports_auth_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NPSSO", "x")
    monkeypatch.setenv("PSN_OUTPUT_DIR", str(tmp_path))

    def boom(settings):
        raise AuthError("NPSSO was rejected or has expired")

    monkeypatch.setattr(cli, "sync", boom)
    assert cli.main(["sync"]) == 1
