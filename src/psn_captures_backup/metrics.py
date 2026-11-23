from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

from .sync import SyncResult

log = logging.getLogger(__name__)

_REFRESH_WARNING_SECONDS = 3 * 24 * 3600


@dataclass
class MetricsState:
    auth_ok: bool = True
    last_auth_success: float = 0.0
    last_sync_success: float = 0.0
    captures_not_ready: int = 0
    failures_total: int = 0

    def record_success(self, result: SyncResult, now: float) -> None:
        self.auth_ok = True
        self.last_auth_success = now
        self.last_sync_success = now
        self.captures_not_ready = result.not_ready
        self.failures_total += result.failed


def token_expiries(token_file: Path) -> tuple[float, float] | None:
    """Return (access_expires_at, refresh_expires_at) from the token file."""
    try:
        data = json.loads(token_file.read_text())
        return float(data["expires_at"]), float(data["refresh_expires_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def render_metrics(state: MetricsState, token_file: Path) -> str:
    lines = [
        "# HELP psn_auth_ok Whether the last sync authenticated successfully.",
        "# TYPE psn_auth_ok gauge",
        f"psn_auth_ok {1 if state.auth_ok else 0}",
        "# HELP psn_auth_last_success_timestamp_seconds Unix time of the last successful auth.",
        "# TYPE psn_auth_last_success_timestamp_seconds gauge",
        f"psn_auth_last_success_timestamp_seconds {state.last_auth_success:.0f}",
        "# HELP psn_sync_last_success_timestamp_seconds Unix time of the last successful sync.",
        "# TYPE psn_sync_last_success_timestamp_seconds gauge",
        f"psn_sync_last_success_timestamp_seconds {state.last_sync_success:.0f}",
        "# HELP psn_sync_captures_not_ready Captures skipped while their transcode is incomplete.",
        "# TYPE psn_sync_captures_not_ready gauge",
        f"psn_sync_captures_not_ready {state.captures_not_ready}",
        "# HELP psn_sync_failures_total Download failures since process start.",
        "# TYPE psn_sync_failures_total counter",
        f"psn_sync_failures_total {state.failures_total}",
    ]
    expiries = token_expiries(token_file)
    if expiries is None:
        lines += [
            "# HELP psn_token_file_present Whether the PSN token file could be read.",
            "# TYPE psn_token_file_present gauge",
            "psn_token_file_present 0",
        ]
    else:
        access_expires, refresh_expires = expiries
        lines += [
            "# HELP psn_token_file_present Whether the PSN token file could be read.",
            "# TYPE psn_token_file_present gauge",
            "psn_token_file_present 1",
            "# HELP psn_token_access_expires_timestamp_seconds Access token expiry.",
            "# TYPE psn_token_access_expires_timestamp_seconds gauge",
            f"psn_token_access_expires_timestamp_seconds {access_expires:.0f}",
            "# HELP psn_token_refresh_expires_timestamp_seconds Refresh token expiry.",
            "# TYPE psn_token_refresh_expires_timestamp_seconds gauge",
            f"psn_token_refresh_expires_timestamp_seconds {refresh_expires:.0f}",
        ]
    return "\n".join(lines) + "\n"


def refresh_expiry_warning(token_file: Path, now: float) -> str | None:
    expiries = token_expiries(token_file)
    if expiries is None:
        return None
    refresh_expires = expiries[1]
    if refresh_expires and refresh_expires - now < _REFRESH_WARNING_SECONDS:
        days = max(0.0, (refresh_expires - now) / 86400)
        return (
            f"PSN refresh token expires in {days:.1f} days; run 'auth' with a fresh "
            "NPSSO, or keep the daemon running so each sync rotates it"
        )
    return None


class _MetricsServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], render: Callable[[], str]) -> None:
        super().__init__(address, _MetricsHandler)
        self.render = render


class _MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = cast(_MetricsServer, self.server).render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        log.debug("metrics: " + format, *args)


class MetricsServer:
    def __init__(self, address: str, state: MetricsState, token_file: Path) -> None:
        host, separator, port = address.rpartition(":")
        if not separator or not port.isdigit():
            raise ValueError(f"invalid metrics address {address!r}; expected HOST:PORT")
        self._address = address
        self._server = _MetricsServer(
            (host, int(port)), lambda: render_metrics(state, token_file)
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="metrics", daemon=True
        )

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        self._thread.start()
        log.info("serving Prometheus metrics on %s", self._address)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
