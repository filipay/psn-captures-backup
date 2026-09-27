from pathlib import Path

import pytest

from psn_captures_backup.config import ConfigError, Settings


def test_from_env_requires_npsso() -> None:
    with pytest.raises(ConfigError, match="NPSSO"):
        Settings.from_env({})


def test_from_env_defaults() -> None:
    settings = Settings.from_env({"NPSSO": "abc", "PSN_OUTPUT_DIR": "/tmp/out"})
    assert settings.npsso == "abc"
    assert settings.output_dir == Path("/tmp/out")
    assert settings.state_file == Path("/tmp/out/.psn-captures-state.sqlite")
    assert settings.token_file == Path("/tmp/out/.psn-token.json")
    assert settings.poll_interval == 21600
    assert settings.max_concurrency == 2
    assert settings.upload_concurrency == 2
    assert settings.upload_queue_limit == 4
    assert settings.include_images is True
    assert settings.include_videos is True


def test_from_env_rejects_bad_numbers() -> None:
    with pytest.raises(ConfigError, match="PSN_POLL_INTERVAL"):
        Settings.from_env({"NPSSO": "abc", "PSN_POLL_INTERVAL": "soon"})


def test_with_overrides_ignores_none() -> None:
    settings = Settings.from_env({"NPSSO": "abc"})
    updated = settings.with_overrides(flat=True, log_level=None)
    assert updated.flat is True
    assert updated.log_level == settings.log_level
