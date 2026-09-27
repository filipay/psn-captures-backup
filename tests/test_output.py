from pathlib import Path

import pytest

from psn_captures_backup.models import Capture
from psn_captures_backup.output import destination, extension_for, sanitize_title, write_atomic


def _capture(**overrides):
    base = {
        "id": "psn123",
        "ugc_type": 1,
        "game_title": "Marvel’s Spider-Man 2",
        "upload_date": __import__("datetime").datetime(
            2025, 10, 11, tzinfo=__import__("datetime").timezone.utc
        ),
        "file_type": "JPEG",
    }
    base.update(overrides)
    return Capture(**base)


def test_sanitize_strips_illegal_characters() -> None:
    assert sanitize_title('Bad:/\\Name*?"<>|') == "Bad___Name______"
    assert sanitize_title("  trailing.  ") == "trailing"
    assert sanitize_title("") == "Unknown Game"


def test_extension_from_file_type() -> None:
    assert extension_for(_capture(file_type="JPEG")) == ".jpg"
    assert extension_for(_capture(file_type="WEBM", ugc_type=2)) == ".webm"
    assert extension_for(_capture(file_type=None, ugc_type=2)) == ".mp4"
    assert extension_for(_capture(file_type=None, ugc_type=1)) == ".jpg"


def test_destination_layout(tmp_path: Path) -> None:
    capture = _capture()
    path = destination(tmp_path, capture, ".jpg")
    assert path == tmp_path / "Marvel’s Spider-Man 2" / "2025-10-11_psn123.jpg"


def test_destination_flat(tmp_path: Path) -> None:
    capture = _capture()
    path = destination(tmp_path, capture, ".jpg", flat=True)
    assert path == tmp_path / "2025-10-11_psn123.jpg"


def test_write_atomic_returns_digest_and_size(tmp_path: Path) -> None:
    dest = tmp_path / "sub" / "file.bin"
    sha256, size = write_atomic(dest, [b"hello", b" world"])
    assert dest.read_bytes() == b"hello world"
    assert size == 11
    assert sha256 == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


def test_write_atomic_cleans_up_on_error(tmp_path: Path) -> None:
    dest = tmp_path / "file.bin"

    def failing_chunks():
        yield b"a"
        raise RuntimeError("stream failed")

    with pytest.raises(RuntimeError):
        write_atomic(dest, failing_chunks())
    assert not dest.exists()
    assert not (tmp_path / "file.bin.part").exists()



def test_destination_uses_capture_timestamp(tmp_path: Path) -> None:
    from datetime import datetime

    capture = _capture(capture_date=datetime(2026, 9, 26, 23, 53, 51))  # noqa: DTZ001
    path = destination(tmp_path, capture, ".webm")
    assert path == (
        tmp_path
        / "Marvel’s Spider-Man 2"
        / "2026-09-26_23-53-51_psn123.webm"
    )
