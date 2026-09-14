from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

AUTH_BASE_URL = "https://ca.account.sony.com/api/authz/v3/oauth"
AUTHORIZE_URL = f"{AUTH_BASE_URL}/authorize"
TOKEN_URL = f"{AUTH_BASE_URL}/token"
NPSSO_URL = "https://ca.account.sony.com/api/v1/ssocookie"
CLIENT_ID = "09515159-7237-4370-9b40-3806e67c0891"
REDIRECT_URI = "com.scee.psxandroid.scecompcall://redirect"
SCOPE = "psn:mobile.v2.core psn:clientapp"
# base64("<CLIENT_ID>:<CLIENT_SECRET>"); literal from psn-api / ps-captures.
BASIC_TOKEN = (
    "MDk1MTUxNTktNzIzNy00MzcwLTliNDAtMzgwNmU2N2MwODkxOnVjUGprYTV0bnRCMktxc1A="
)

_EXPIRY_SKEW_SECONDS = 60


class AuthError(RuntimeError):
    """Raised when the NPSSO/refresh-token flow cannot produce an access token."""


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float
    refresh_expires_at: float

    def to_json(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "refresh_expires_at": self.refresh_expires_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Tokens:
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=float(data.get("expires_at", 0)),
            refresh_expires_at=float(data.get("refresh_expires_at", 0)),
        )


def _tokens_from_response(payload: dict[str, Any]) -> Tokens:
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    if not access or not refresh:
        raise AuthError("token response did not include access/refresh tokens")
    now = time.time()
    return Tokens(
        access_token=access,
        refresh_token=refresh,
        expires_at=now + float(payload.get("expires_in", 3600)),
        refresh_expires_at=now + float(payload.get("refresh_token_expires_in", 0)),
    )


class TokenManager:
    def __init__(
        self, npsso: str, token_file: Path, client: httpx.Client | None = None
    ) -> None:
        self._npsso = npsso
        self._token_file = token_file
        self._client = client or httpx.Client(
            timeout=30.0,
            follow_redirects=False,
            headers={"User-Agent": "curl/8.12.1"},
        )
        self._tokens: Tokens | None = None

    def close(self) -> None:
        self._client.close()

    def get_access_token(self) -> str:
        tokens = self._tokens or self._load()
        if tokens is None:
            tokens = self._exchange_npsso()
        elif tokens.expires_at - _EXPIRY_SKEW_SECONDS <= time.time():
            try:
                tokens = self._refresh(tokens)
            except AuthError:
                tokens = self._exchange_npsso()
        self._tokens = tokens
        self._save(tokens)
        return tokens.access_token

    def _load(self) -> Tokens | None:
        if not self._token_file.exists():
            return None
        try:
            data = json.loads(self._token_file.read_text())
            return Tokens.from_json(data)
        except (ValueError, KeyError):
            return None

    def _save(self, tokens: Tokens) -> None:
        self._token_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(tokens.to_json()))
        os.chmod(self._token_file, 0o600)

    def _exchange_npsso(self) -> Tokens:
        response = self._client.get(
            AUTHORIZE_URL,
            params={
                "access_type": "offline",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "scope": SCOPE,
            },
            headers={"Cookie": f"npsso={self._npsso}"},
        )
        location = response.headers.get("location", "")
        if "/redirect" not in location:
            raise AuthError(
                "NPSSO was rejected or has expired; obtain a fresh NPSSO and update .env"
            )
        code = httpx.URL(location).params.get("code")
        if not code:
            raise AuthError("authorization redirect did not contain a code")
        return self._request_tokens(
            {
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
                "token_format": "jwt",
            }
        )

    def _refresh(self, tokens: Tokens) -> Tokens:
        return self._request_tokens(
            {
                "refresh_token": tokens.refresh_token,
                "grant_type": "refresh_token",
                "token_format": "jwt",
                "scope": SCOPE,
            }
        )

    def _request_tokens(self, data: dict[str, str]) -> Tokens:
        response = self._client.post(
            TOKEN_URL,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {BASIC_TOKEN}",
            },
        )
        if response.status_code >= 400:
            raise AuthError(f"token request failed with HTTP {response.status_code}")
        return _tokens_from_response(response.json())
