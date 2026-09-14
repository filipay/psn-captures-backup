from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .auth import AuthError, TokenManager
from .config import ConfigError, Settings
from .psn import PsnClient, PsnError
from .state import StateError
from .sync import sync

log = logging.getLogger("psn_captures_backup")


def _build_tokens(settings: Settings) -> TokenManager:
    return TokenManager(settings.npsso, settings.token_file)


def _build_psn(access_token: str) -> PsnClient:
    return PsnClient(access_token)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level, logging.INFO)
    if settings.json_log:
        handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter())
        logging.basicConfig(level=level, handlers=[handler], force=True)
        return
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="psn-captures-backup",
        description="Back up PlayStation captures from PSN before the 14-day cloud expiry.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("sync", "daemon", "list"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--output-dir")
        cmd.add_argument("--state-file")
        cmd.add_argument("--token-file")
        cmd.add_argument("--flat", action="store_true", default=None)
        cmd.add_argument("--post-download-script")
        cmd.add_argument("--max-concurrency", type=int)
        cmd.add_argument("--json-log", action="store_true", default=None)
        cmd.add_argument("--verbose", action="store_true")
        if name in ("sync", "daemon", "list"):
            cmd.add_argument("--no-images", action="store_true")
            cmd.add_argument("--no-videos", action="store_true")
        if name == "sync":
            cmd.add_argument("--dry-run", action="store_true", default=None)
        if name == "daemon":
            cmd.add_argument("--interval", type=int)

    auth = sub.add_parser("auth")
    auth.add_argument("--check", action="store_true")
    auth.add_argument("--token-file")
    return parser


def _settings_for(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()

    def _path(name: str) -> Path | None:
        value = getattr(args, name, None)
        return Path(value).expanduser() if value else None

    overrides: dict[str, object] = {
        "output_dir": _path("output_dir"),
        "state_file": _path("state_file"),
        "token_file": _path("token_file"),
        "flat": getattr(args, "flat", None),
        "post_download_script": _path("post_download_script"),
        "max_concurrency": getattr(args, "max_concurrency", None),
        "json_log": getattr(args, "json_log", None),
        "dry_run": getattr(args, "dry_run", None),
    }
    if getattr(args, "no_images", False):
        overrides["include_images"] = False
    if getattr(args, "no_videos", False):
        overrides["include_videos"] = False
    if getattr(args, "verbose", False):
        overrides["log_level"] = "DEBUG"

    max_concurrency = overrides.get("max_concurrency")
    if isinstance(max_concurrency, int) and max_concurrency <= 0:
        raise ConfigError("--max-concurrency must be positive")
    interval = getattr(args, "interval", None)
    if isinstance(interval, int) and interval <= 0:
        raise ConfigError("--interval must be positive")

    return settings.with_overrides(**overrides)


def _cmd_auth(settings: Settings, args: argparse.Namespace) -> int:
    tokens = _build_tokens(settings)
    try:
        access_token = tokens.get_access_token()
    except AuthError as exc:
        log.error("%s", exc)
        return 1
    finally:
        tokens.close()
    if args.check:
        print("Authentication OK")
    else:
        print(f"Access token acquired (length {len(access_token)})")
    return 0


def _cmd_list(settings: Settings, args: argparse.Namespace) -> int:
    tokens = _build_tokens(settings)
    psn: PsnClient | None = None
    try:
        psn = _build_psn(tokens.get_access_token())
        captures = psn.list_captures()
    except (AuthError, PsnError, httpx.HTTPError, OSError) as exc:
        log.error("%s", exc)
        return 1
    finally:
        if psn is not None:
            psn.close()
        tokens.close()

    print(f"{len(captures)} capture(s)")
    for capture in captures:
        kind = "image" if capture.is_image else "video"
        print(
            f"{capture.id}  {kind:5}  {capture.upload_date:%Y-%m-%d}  "
            f"{capture.game_title}  {capture.resolution or '-'}  {capture.file_size or '-'}"
        )
    return 0


def _cmd_sync(settings: Settings, args: argparse.Namespace) -> int:
    try:
        result = sync(settings)
    except (AuthError, PsnError, StateError, httpx.HTTPError, OSError) as exc:
        log.error("%s", exc)
        return 1
    print(
        f"listed={result.listed} downloaded={result.downloaded} "
        f"skipped={result.skipped} failed={result.failed}"
    )
    return 1 if result.failed else 0


def _cmd_daemon(settings: Settings, args: argparse.Namespace) -> int:
    interval = args.interval or settings.poll_interval
    stopping = False

    def _stop(signum, frame):  # type: ignore[no-untyped-def]
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stopping:
        try:
            result = sync(settings)
            log.info(
                "sync complete: listed=%s downloaded=%s skipped=%s failed=%s",
                result.listed,
                result.downloaded,
                result.skipped,
                result.failed,
            )
        except AuthError as exc:
            log.error("authentication failed: %s", exc)
        except Exception as exc:  # noqa: BLE001 - keep the daemon alive
            log.error("sync failed: %s", exc)
        for _ in range(interval):
            if stopping:
                break
            time.sleep(1)
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        settings = _settings_for(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _configure_logging(settings)
    handlers = {
        "auth": _cmd_auth,
        "list": _cmd_list,
        "sync": _cmd_sync,
        "daemon": _cmd_daemon,
    }
    return handlers[args.command](settings, args)
