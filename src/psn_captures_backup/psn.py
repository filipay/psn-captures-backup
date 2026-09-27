from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

import httpx

from .models import Capture, CaptureParseError
from .output import write_atomic

log = logging.getLogger(__name__)

PSN_BASE_URL = (
    "https://m.np.playstation.com/api/gameMediaService/v2/c2s"
    "/category/cloudMediaGallery/ugcType/all"
)
GAME_LIST_BASE_URL = "https://m.np.playstation.net/api/gamelist/v2/users"
_TERMINAL_CURSOR = "-1"


class PsnError(RuntimeError):
    """Raised when the PSN API returns an unusable response."""


def _extract_cloudfront_cookies(response: httpx.Response) -> str:
    pairs: list[str] = []
    for header in response.headers.get_list("set-cookie"):
        if header.lower().startswith("cloudfront"):
            pairs.append(header.split(";")[0])
    return "; ".join(pairs)


class PsnClient:
    def __init__(self, access_token: str, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=60.0)
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self.cloudfront_cookies = ""
        self._title_cache: dict[tuple[str, str], str | None] = {}

    def close(self) -> None:
        self._client.close()

    def list_captures(self) -> list[Capture]:
        captures: list[Capture] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params = {"includeTokenizedUrls": "true", "limit": "100"}
            if cursor:
                params["nextCursorMark"] = cursor
            response = self._client.get(PSN_BASE_URL, params=params, headers=self._headers)
            if response.status_code == 401:
                raise PsnError("PSN rejected the access token")
            if response.status_code >= 400:
                raise PsnError(f"capture listing failed with HTTP {response.status_code}")
            cookies = _extract_cloudfront_cookies(response)
            if cookies:
                self.cloudfront_cookies = cookies
            payload = response.json()
            for item in payload.get("ugcDocument", []):
                try:
                    capture = Capture.from_api(item)
                except CaptureParseError:
                    continue
                if capture.game_title == "Unknown Game" and capture.title_id:
                    account_id = item.get("sceUserAccountId")
                    if isinstance(account_id, str) and account_id:
                        resolved = self._lookup_title_name(account_id, capture.title_id)
                        if resolved:
                            capture = replace(capture, game_title=resolved)
                captures.append(capture)
            next_cursor = payload.get("nextCursorMark")
            if not next_cursor or next_cursor == _TERMINAL_CURSOR or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return captures

    def _lookup_title_name(self, account_id: str, title_id: str) -> str | None:
        cache_key = (account_id, title_id)
        if cache_key in self._title_cache:
            return self._title_cache[cache_key]

        try:
            response = self._client.get(
                f"{GAME_LIST_BASE_URL}/{account_id}/titles/{title_id}",
                headers=self._headers,
            )
            if response.status_code >= 400:
                log.warning(
                    "PSN game title lookup failed for %s: HTTP %s",
                    title_id,
                    response.status_code,
                )
                self._title_cache[cache_key] = None
                return None
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("PSN game title lookup failed for %s: %s", title_id, exc)
            self._title_cache[cache_key] = None
            return None

        for key in ("localizedName", "name"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                resolved = value.strip()
                self._title_cache[cache_key] = resolved
                return resolved

        self._title_cache[cache_key] = None
        return None

    def download_to(self, capture: Capture, dest: Path) -> tuple[str, int]:
        if capture.is_video:
            url = capture.download_url or capture.video_url
        else:
            url = capture.screenshot_url
        if not url:
            raise PsnError(f"no downloadable URL for capture {capture.id}")
        headers = {"Cookie": self.cloudfront_cookies} if self.cloudfront_cookies else {}
        with self._client.stream("GET", url, headers=headers) as response:
            if response.status_code >= 400:
                raise PsnError(
                    f"download failed with HTTP {response.status_code} for {capture.id}"
                )
            return write_atomic(dest, response.iter_bytes(256 * 1024))
