from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from pathlib import Path

from .models import Capture

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TRAILING = re.compile(r"[. ]+$")
_FILE_TYPE_EXT = {"JPEG": ".jpg", "JPG": ".jpg", "PNG": ".png", "MP4": ".mp4", "WEBM": ".webm"}


def sanitize_title(title: str) -> str:
    cleaned = _ILLEGAL.sub("_", title).strip()
    cleaned = _TRAILING.sub("", cleaned)
    cleaned = cleaned[:120]
    return cleaned or "Unknown Game"


def extension_for(capture: Capture) -> str:
    if capture.file_type:
        known = _FILE_TYPE_EXT.get(capture.file_type.upper())
        if known:
            return known
    return ".mp4" if capture.is_video else ".jpg"


def destination(output_dir: Path, capture: Capture, ext: str, *, flat: bool = False) -> Path:
    folder = output_dir if flat else output_dir / sanitize_title(capture.game_title)
    stamp = capture.upload_date.strftime("%Y-%m-%d")
    return folder / f"{stamp}_{capture.id}{ext}"


def write_atomic(dest: Path, chunks: Iterable[bytes]) -> tuple[str, int]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    size = 0
    try:
        with open(tmp, "wb") as handle:
            for chunk in chunks:
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return digest.hexdigest(), size
