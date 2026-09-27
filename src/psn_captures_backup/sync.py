from __future__ import annotations

import logging
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
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


def _run_hook(script: Path, paths: list[Path]) -> None:
    try:
        completed = subprocess.run(
            [str(script), *(str(path) for path in paths)],
            check=False,
            timeout=300,
        )
        if completed.returncode:
            log.warning(
                "post-download hook exited with status %s for %s capture(s)",
                completed.returncode,
                len(paths),
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("post-download hook failed for %s capture(s): %s", len(paths), exc)


class _PostDownloadHookDispatcher:
    """Debounce and batch post-download hooks while keeping queue limits."""

    def __init__(
        self,
        script: Path,
        debounce_seconds: float,
        concurrency: int,
        queue_limit: int,
    ) -> None:
        self._script = script
        self._debounce_seconds = debounce_seconds
        self._queue_limit = queue_limit
        self._executor = ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="post-download-hook",
        )
        self._condition = threading.Condition()
        self._pending: list[Path] = []
        self._deadline: float | None = None
        self._closed = False
        self._futures: set[Future[None]] = set()
        self._thread = threading.Thread(
            target=self._run,
            name="post-download-hook-dispatcher",
            daemon=True,
        )
        self._thread.start()

    def submit(self, path: Path) -> None:
        with self._condition:
            self._pending.append(path)
            self._deadline = time.monotonic() + self._debounce_seconds
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify()
        self._thread.join()
        self._executor.shutdown(wait=True)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._closed:
                    self._condition.wait()

                if self._pending:
                    deadline = self._deadline or time.monotonic()
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        self._condition.wait(timeout=remaining)
                        continue

                    batch = self._pending
                    self._pending = []
                    self._deadline = None
                elif self._closed:
                    break
                else:
                    continue

            self._submit_batch(batch)

    def _submit_batch(self, paths: list[Path]) -> None:
        while len(self._futures) >= self._queue_limit:
            done, _ = wait(self._futures, return_when=FIRST_COMPLETED)
            self._futures.difference_update(done)
            for future in done:
                future.result()

        self._futures.add(self._executor.submit(_run_hook, self._script, paths))


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

        with (
            ThreadPoolExecutor(max_workers=settings.max_concurrency) as download_pool,
            ThreadPoolExecutor(max_workers=settings.upload_concurrency) as upload_pool,
        ):
            hook_dispatcher = (
                _PostDownloadHookDispatcher(
                    settings.post_download_script,
                    settings.post_download_debounce_seconds,
                    settings.upload_concurrency,
                    settings.upload_queue_limit,
                )
                if settings.post_download_script
                and settings.post_download_debounce_seconds > 0
                else None
            )
            pending_downloads = iter(pending)
            downloads: dict[Future[tuple[Path, str, int]], Capture] = {}
            uploads: set[Future[None]] = set()

            def submit_download() -> None:
                try:
                    capture = next(pending_downloads)
                except StopIteration:
                    return
                future = download_pool.submit(
                    _download_one, psn, settings, capture  # type: ignore[arg-type]
                )
                downloads[future] = capture

            for _ in range(min(settings.max_concurrency, len(pending))):
                submit_download()

            while downloads or uploads:
                if downloads:
                    done_downloads, _ = wait(downloads, return_when=FIRST_COMPLETED)
                    for future in done_downloads:
                        capture = downloads.pop(future)
                        try:
                            dest, sha256, size = future.result()
                        except (PsnError, OSError) as exc:
                            log.warning("download failed for %s: %s", capture.id, exc)
                            state.record_failed(capture, str(exc))
                            result.failed += 1
                        else:
                            state.record_downloaded(capture, dest, sha256, size)
                            result.downloaded += 1
                            log.info("downloaded %s -> %s", capture.id, dest)

                            if settings.post_download_script:
                                if hook_dispatcher is not None:
                                    hook_dispatcher.submit(dest)
                                else:
                                    while len(uploads) >= settings.upload_queue_limit:
                                        done_uploads, _ = wait(
                                            uploads, return_when=FIRST_COMPLETED
                                        )
                                        uploads.difference_update(done_uploads)
                                        for completed in done_uploads:
                                            completed.result()
                                    uploads.add(
                                        upload_pool.submit(
                                            _run_hook, settings.post_download_script, [dest]
                                        )
                                    )

                        submit_download()
                elif uploads:
                    done_uploads, _ = wait(uploads, return_when=FIRST_COMPLETED)
                    uploads.difference_update(done_uploads)
                    for completed in done_uploads:
                        completed.result()

            if hook_dispatcher is not None:
                hook_dispatcher.close()
    finally:
        if owns_state:
            state.close()
        if owns_psn and psn is not None:
            psn.close()  # type: ignore[attr-defined]
        if owns_tokens:
            tokens.close()  # type: ignore[attr-defined]
    return result
