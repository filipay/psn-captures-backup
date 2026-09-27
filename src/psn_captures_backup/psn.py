from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

import httpx

from .models import Capture, CaptureParseError
from .output import write_atomic

log = logging.getLogger(__name__)

STORE_TITLE_URL = (
    "https://store.playstation.com/store/api/chihiro/00_09_000/titlecontainer/GB/en/999"
)

PSN_BASE_URL = (
    "https://m.np.playstation.com/api/gameMediaService/v2/c2s"
    "/category/cloudMediaGallery/ugcType/all"
)
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
        self._title_cache: dict[str, str | None] = {}

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
                    resolved = self._lookup_title_name(capture.title_id)
                    if resolved:
                        capture = replace(capture, game_title=resolved)
                captures.append(capture)
            next_cursor = payload.get("nextCursorMark")
            if not next_cursor or next_cursor == _TERMINAL_CURSOR or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return captures

    def _lookup_title_name(self, title_id: str) -> str | None:
        if title_id in self._title_cache:
            return self._title_cache[title_id]

        normalized = title_id if "_" in title_id else f"{title_id}_00"
        try:
            response = self._client.get(f"{STORE_TITLE_URL}/{normalized}")
            if response.status_code >= 400:
                log.warning(
                    "PS Store title lookup failed for %s: HTTP %s",
                    title_id,
                    response.status_code,
                )
                self._title_cache[title_id] = None
                return None
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("PS Store title lookup failed for %s: %s", title_id, exc)
            self._title_cache[title_id] = None
            return None

        for key in ("name", "localizedName", "title"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                self._title_cache[title_id] = value.strip()
                return value.strip()

        self._title_cache[title_id] = None
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
