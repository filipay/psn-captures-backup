from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .models import Capture

_SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    local_path TEXT,
    sha256 TEXT,
    size INTEGER,
    upload_date TEXT,
    game_title TEXT,
    first_seen TEXT NOT NULL,
    last_attempt TEXT
);
"""


class StateError(RuntimeError):
    """Raised when the state database cannot be opened or written."""


@dataclass(frozen=True)
class StateRow:
    id: str
    status: str
    local_path: str | None


def _now() -> str:
    return datetime.now(UTC).isoformat()


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        conn: sqlite3.Connection | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            conn.commit()
        except (OSError, sqlite3.Error) as exc:
            if conn is not None:
                conn.close()
            raise StateError(
                f"cannot open state database {path}: {exc}; "
                f"check that {path.parent} exists and is writable by the current user"
            ) from exc
        assert conn is not None
        self._conn = conn

    def get(self, capture_id: str) -> StateRow | None:
        row = self._conn.execute(
            "SELECT id, status, local_path FROM captures WHERE id = ?", (capture_id,)
        ).fetchone()
        if row is None:
            return None
        return StateRow(id=row["id"], status=row["status"], local_path=row["local_path"])

    def is_done(self, capture_id: str) -> bool:
        row = self.get(capture_id)
        return row is not None and row.status == "downloaded"

    def record_downloaded(
        self, capture: Capture, local_path: Path, sha256: str, size: int
    ) -> None:
        now = _now()
        self._conn.execute(
            """
            INSERT INTO captures (id, status, local_path, sha256, size, upload_date,
                                  game_title, first_seen, last_attempt)
            VALUES (?, 'downloaded', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = 'downloaded',
                local_path = excluded.local_path,
                sha256 = excluded.sha256,
                size = excluded.size,
                last_attempt = excluded.last_attempt
            """,
            (
                capture.id,
                str(local_path),
                sha256,
                size,
                capture.upload_date.isoformat(),
                capture.game_title,
                now,
                now,
            ),
        )
        self._conn.commit()

    def record_failed(self, capture: Capture, error: str) -> None:
        now = _now()
        self._conn.execute(
            """
            INSERT INTO captures (id, status, local_path, upload_date, game_title,
                                  first_seen, last_attempt)
            VALUES (?, 'failed', NULL, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET status = 'failed', last_attempt = excluded.last_attempt
            """,
            (capture.id, capture.upload_date.isoformat(), capture.game_title, now, now),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
