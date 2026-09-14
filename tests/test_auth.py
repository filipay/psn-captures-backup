import json
import time
from pathlib import Path

import httpx
import pytest

from psn_captures_backup.auth import AuthError, TokenManager


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_exchanges_npsso_and_persists_tokens(tmp_path: Path) -> None:
    calls = {"authorize": 0, "token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authorize"):
            calls["authorize"] += 1
            return httpx.Response(
                302,
                headers={
                    "location": "com.scee.psxandroid.scecompcall://redirect/?code=THECODE"
                },
            )
        calls["token"] += 1
        return httpx.Response(
            200,
            json={
                "access_token": "ACCESS",
                "refresh_token": "REFRESH",
                "expires_in": 3600,
                "refresh_token_expires_in": 5184000,
            },
        )

    manager = TokenManager("NPSSO", tmp_path / "token.json", client=_client(handler))
    assert manager.get_access_token() == "ACCESS"
    assert calls == {"authorize": 1, "token": 1}
    saved = json.loads((tmp_path / "token.json").read_text())
    assert saved["refresh_token"] == "REFRESH"
    assert (tmp_path / "token.json").stat().st_mode & 0o777 == 0o600
    manager.close()


def test_uses_valid_cached_token_without_network(tmp_path: Path) -> None:
    token_file = tmp_path / "token.json"
    token_file.write_text(
        json.dumps(
            {
                "access_token": "CACHED",
                "refresh_token": "REFRESH",
                "expires_at": time.time() + 3600,
                "refresh_expires_at": 0,
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not hit the network")

    manager = TokenManager("NPSSO", token_file, client=_client(handler))
    assert manager.get_access_token() == "CACHED"
    manager.close()


def test_refreshes_expired_access_token(tmp_path: Path) -> None:
    token_file = tmp_path / "token.json"
    token_file.write_text(
        json.dumps(
            {
                "access_token": "OLD",
                "refresh_token": "REFRESH",
                "expires_at": time.time() - 10,
                "refresh_expires_at": 0,
            }
        )
    )
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content.decode())
        return httpx.Response(
            200,
            json={"access_token": "NEW", "refresh_token": "REFRESH2", "expires_in": 3600},
        )

    manager = TokenManager("NPSSO", token_file, client=_client(handler))
    assert manager.get_access_token() == "NEW"
    assert "grant_type=refresh_token" in bodies[0]
    manager.close()


def test_falls_back_to_npsso_when_refresh_fails(tmp_path: Path) -> None:
    token_file = tmp_path / "token.json"
    token_file.write_text(
        json.dumps(
            {
                "access_token": "OLD",
                "refresh_token": "DEAD",
                "expires_at": time.time() - 10,
                "refresh_expires_at": 0,
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authorize"):
            return httpx.Response(
                302,
                headers={"location": "com.scee.psxandroid.scecompcall://redirect/?code=NEWCODE"},
            )
        if "grant_type=refresh_token" in request.content.decode():
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"access_token": "FRESH", "refresh_token": "R3"})

    manager = TokenManager("NPSSO", token_file, client=_client(handler))
    assert manager.get_access_token() == "FRESH"
    manager.close()


def test_expired_npsso_raises(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://ca.account.sony.com/error"})

    manager = TokenManager("STALE", tmp_path / "token.json", client=_client(handler))
    with pytest.raises(AuthError, match="NPSSO"):
        manager.get_access_token()
    manager.close()
