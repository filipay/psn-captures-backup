import json
from pathlib import Path

import httpx

from psn_captures_backup.metrics import (
    MetricsServer,
    MetricsState,
    refresh_expiry_warning,
    render_metrics,
)
from psn_captures_backup.sync import SyncResult


def _token_file(tmp_path: Path, access: float, refresh: float) -> Path:
    path = tmp_path / "token.json"
    path.write_text(
        json.dumps(
            {
                "access_token": "SECRET-ACCESS",
                "refresh_token": "SECRET-REFRESH",
                "expires_at": access,
                "refresh_expires_at": refresh,
            }
        )
    )
    return path


def test_render_metrics_includes_expiries_without_secrets(tmp_path: Path) -> None:
    token_file = _token_file(tmp_path, access=1000, refresh=2000)
    state = MetricsState()
    state.record_success(
        SyncResult(listed=3, downloaded=1, skipped=1, failed=1, not_ready=1),
        now=500.0,
    )
    text = render_metrics(state, token_file)
    assert "psn_auth_ok 1" in text
    assert "psn_auth_last_success_timestamp_seconds 500" in text
    assert "psn_sync_captures_not_ready 1" in text
    assert "psn_sync_failures_total 1" in text
    assert "psn_token_file_present 1" in text
    assert "psn_token_access_expires_timestamp_seconds 1000" in text
    assert "psn_token_refresh_expires_timestamp_seconds 2000" in text
    assert "SECRET" not in text


def test_render_metrics_reports_missing_token_file(tmp_path: Path) -> None:
    text = render_metrics(MetricsState(), tmp_path / "missing.json")
    assert "psn_token_file_present 0" in text
    assert "psn_token_refresh_expires_timestamp_seconds" not in text


def test_refresh_expiry_warning_only_when_near(tmp_path: Path) -> None:
    token_file = _token_file(tmp_path, access=0, refresh=2 * 86400)
    warning = refresh_expiry_warning(token_file, now=0.0)
    assert warning is not None
    assert "2.0 days" in warning

    token_file = _token_file(tmp_path, access=0, refresh=10 * 86400)
    assert refresh_expiry_warning(token_file, now=0.0) is None


def test_metrics_server_serves_prometheus_text(tmp_path: Path) -> None:
    token_file = _token_file(tmp_path, access=1000, refresh=2000)
    server = MetricsServer("127.0.0.1:0", MetricsState(), token_file)
    server.start()
    try:
        response = httpx.get(f"http://127.0.0.1:{server.port}/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "psn_auth_ok 1" in response.text
        assert httpx.get(f"http://127.0.0.1:{server.port}/nope").status_code == 404
    finally:
        server.close()
