from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"invalid boolean value: {value!r}")


@dataclass(frozen=True)
class Settings:
    npsso: str
    output_dir: Path
    state_file: Path
    token_file: Path
    poll_interval: int = 21600
    max_concurrency: int = 2
    upload_concurrency: int = 2
    upload_queue_limit: int = 4
    include_images: bool = True
    include_videos: bool = True
    log_level: str = "INFO"
    flat: bool = False
    post_download_script: Path | None = None
    post_download_debounce_seconds: float = 0.0
    dry_run: bool = False
    json_log: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if env is None else env
        npsso = values.get("NPSSO", "").strip()
        if not npsso:
            raise ConfigError("NPSSO is required (set it in .env or the environment)")

        output_dir = Path(values.get("PSN_OUTPUT_DIR", "./captures")).expanduser()
        state_file = Path(
            values.get("PSN_STATE_FILE", str(output_dir / ".psn-captures-state.sqlite"))
        ).expanduser()
        token_file = Path(
            values.get("PSN_TOKEN_FILE", str(output_dir / ".psn-token.json"))
        ).expanduser()

        try:
            poll_interval = int(values.get("PSN_POLL_INTERVAL", "21600"))
        except ValueError as exc:
            raise ConfigError(f"PSN_POLL_INTERVAL must be an integer: {exc}") from exc
        try:
            max_concurrency = int(values.get("PSN_MAX_CONCURRENCY", "2"))
            upload_concurrency = int(values.get("PSN_UPLOAD_CONCURRENCY", "2"))
            upload_queue_limit = int(values.get("PSN_UPLOAD_QUEUE_LIMIT", "4"))
            post_download_debounce_seconds = float(
                values.get("PSN_POST_DOWNLOAD_DEBOUNCE_SECONDS", "0")
            )
        except ValueError as exc:
            raise ConfigError(f"concurrency/debounce settings must be numeric: {exc}") from exc
        if poll_interval <= 0:
            raise ConfigError("PSN_POLL_INTERVAL must be positive")
        if max_concurrency <= 0:
            raise ConfigError("PSN_MAX_CONCURRENCY must be positive")
        if upload_concurrency <= 0:
            raise ConfigError("PSN_UPLOAD_CONCURRENCY must be positive")
        if upload_queue_limit <= 0:
            raise ConfigError("PSN_UPLOAD_QUEUE_LIMIT must be positive")
        if post_download_debounce_seconds < 0:
            raise ConfigError("PSN_POST_DOWNLOAD_DEBOUNCE_SECONDS must not be negative")

        script = values.get("PSN_POST_DOWNLOAD_SCRIPT")
        return cls(
            npsso=npsso,
            output_dir=output_dir,
            state_file=state_file,
            token_file=token_file,
            poll_interval=poll_interval,
            max_concurrency=max_concurrency,
            upload_concurrency=upload_concurrency,
            upload_queue_limit=upload_queue_limit,
            include_images=_parse_bool(values.get("PSN_INCLUDE_IMAGES", "true")),
            include_videos=_parse_bool(values.get("PSN_INCLUDE_VIDEOS", "true")),
            log_level=values.get("PSN_LOG_LEVEL", "INFO").upper(),
            post_download_script=Path(script).expanduser() if script else None,
        )

    def with_overrides(self, **overrides: Any) -> Settings:
        clean = {key: value for key, value in overrides.items() if value is not None}
        return replace(self, **clean)
