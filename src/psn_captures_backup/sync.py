from __future__ import annotations

import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .auth import TokenManager
from .config import Settings
from .models import Capture
from .output import destination, extension_for
from .psn import PsnClient, PsnError
from .state import StateStore

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    listed: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0


def _wanted(capture: Capture, settings: Settings) -> bool:
    if capture.is_image:
        return settings.include_images
    if capture.is_video:
        return settings.include_videos
    return False


def _already_downloaded(state: StateStore, capture: Capture) -> bool:
    row = state.get(capture.id)
    return (
        row is not None
        and row.status == "downloaded"
        and row.local_path is not None
        and Path(row.local_path).exists()
    )


def _run_hook(script: Path, path: Path) -> None:
    try:
        subprocess.run([str(script), str(path)], check=False, timeout=300)
    except OSError as exc:
        log.warning("post-download hook failed for %s: %s", path, exc)


def _download_one(psn: PsnClient, settings: Settings, capture: Capture) -> tuple[Path, str, int]:
    ext = extension_for(capture)
    dest = destination(settings.output_dir, capture, ext, flat=settings.flat)
    sha256, size = psn.download_to(capture, dest)
    return dest, sha256, size


def sync(
    settings: Settings,
    *,
    tokens: object | None = None,
    psn: object | None = None,
    state: StateStore | None = None,
) -> SyncResult:
    owns_tokens = tokens is None
    owns_psn = psn is None
    owns_state = state is None

    tokens = tokens or TokenManager(settings.npsso, settings.token_file)
    state = state or StateStore(settings.state_file)
    result = SyncResult()
    try:
        access_token = tokens.get_access_token()  # type: ignore[attr-defined]
        psn = psn or PsnClient(access_token)
        captures = psn.list_captures()  # type: ignore[attr-defined]
        result.listed = len(captures)

        pending: list[Capture] = []
        for capture in captures:
            if not _wanted(capture, settings):
                continue
            if _already_downloaded(state, capture):
                result.skipped += 1
                continue
            if settings.dry_run:
                result.skipped += 1
                continue
            pending.append(capture)

        with ThreadPoolExecutor(max_workers=settings.max_concurrency) as pool:
            futures = {
                pool.submit(_download_one, psn, settings, capture): capture  # type: ignore[arg-type]
                for capture in pending
            }
            for future in as_completed(futures):
                capture = futures[future]
                try:
                    dest, sha256, size = future.result()
                except (PsnError, OSError) as exc:
                    log.warning("download failed for %s: %s", capture.id, exc)
                    state.record_failed(capture, str(exc))
                    result.failed += 1
                    continue
                state.record_downloaded(capture, dest, sha256, size)
                result.downloaded += 1
                log.info("downloaded %s -> %s", capture.id, dest)
                if settings.post_download_script:
                    _run_hook(settings.post_download_script, dest)
    finally:
        if owns_state:
            state.close()
        if owns_psn and psn is not None:
            psn.close()  # type: ignore[attr-defined]
        if owns_tokens:
            tokens.close()  # type: ignore[attr-defined]
    return result
