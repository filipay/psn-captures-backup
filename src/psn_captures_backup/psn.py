from __future__ import annotations

import logging
import re
import threading
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

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

_MAX_BODY_EXCERPT = 300
_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_WHITESPACE_PATTERN = re.compile(r"\s+")


class PsnError(RuntimeError):
    """Raised when the PSN API returns an unusable response."""


def _extract_cloudfront_cookies(response: httpx.Response) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for header in response.headers.get_list("set-cookie"):
        name, separator, rest = header.partition("=")
        if separator and name.strip().lower().startswith("cloudfront"):
            cookies[name.strip()] = rest.split(";", 1)[0].strip()
    return cookies


def _parse_cookie_header(value: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for pair in value.split(";"):
        name, separator, rest = pair.strip().partition("=")
        if separator and name:
            cookies[name] = rest.strip()
    return cookies


def _body_excerpt(response: httpx.Response) -> str:
    """Return a short, sanitized error page excerpt safe for logs."""
    try:
        raw = response.read()
    except httpx.HTTPError:
        return ""
    text = raw[:2048].decode("utf-8", "replace")
    text = _URL_PATTERN.sub("<url>", text)
    text = _WHITESPACE_PATTERN.sub(" ", text).strip()
    return text[:_MAX_BODY_EXCERPT]


class PsnClient:
    def __init__(self, access_token: str, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=60.0)
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._cookie_lock = threading.RLock()
        self._cookies: dict[str, str] = {}
        self._cookies_generation = 0
        self._title_cache: dict[tuple[str, str], str | None] = {}

    def close(self) -> None:
        self._client.close()

    @property
    def cloudfront_cookies(self) -> str:
        with self._cookie_lock:
            return "; ".join(f"{name}={value}" for name, value in self._cookies.items())

    @cloudfront_cookies.setter
    def cloudfront_cookies(self, value: str) -> None:
        with self._cookie_lock:
            self._cookies = _parse_cookie_header(value)

    def _merge_cloudfront_cookies(self, response: httpx.Response) -> None:
        cookies = _extract_cloudfront_cookies(response)
        if not cookies:
            return
        with self._cookie_lock:
            self._cookies.update(cookies)
            self._cookies_generation += 1

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
            self._merge_cloudfront_cookies(response)
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

    @staticmethod
    def _download_source(capture: Capture) -> tuple[str, str | None]:
        if capture.is_video:
            if capture.download_url:
                return "download_url", capture.download_url
            return "video_url", capture.video_url
        return "screenshot_url", capture.screenshot_url

    def _describe_failure(
        self, response: httpx.Response, capture: Capture, field: str, url: str
    ) -> str:
        parsed = urlsplit(url)
        with self._cookie_lock:
            cookie_names = ",".join(sorted(self._cookies)) or "none"
        details = [
            f"HTTP {response.status_code} for capture {capture.id}",
            f"field={field}",
            f"host={parsed.hostname}",
            f"path=/{capture.id}/{parsed.path.rsplit('/', 1)[-1]}",
            f"cookies={cookie_names}",
            f"transcode={capture.transcode_status or 'unknown'}",
            f"source={capture.source_of_media or 'unknown'}",
        ]
        for header in ("x-amz-cf-id", "x-cache", "via", "content-type"):
            value = response.headers.get(header)
            if value:
                details.append(f"{header}={value}")
        excerpt = _body_excerpt(response)
        if excerpt:
            details.append(f"body={excerpt}")
        return "download failed with " + " ".join(details)

    def _refresh_download_authorization(self, seen_generation: int) -> None:
        """Re-list the gallery to refresh CloudFront cookies before a retry.

        PSN's signed CloudFront cookies can expire while a long sync is still
        downloading, and they are the only authorization material echoed on the
        media URLs. Hold the lock so concurrent 403s trigger a single refresh.
        """
        with self._cookie_lock:
            if self._cookies_generation != seen_generation:
                return
            response = self._client.get(
                PSN_BASE_URL,
                params={"includeTokenizedUrls": "true", "limit": "1"},
                headers=self._headers,
            )
            if response.status_code == 401:
                raise PsnError(
                    "PSN rejected the access token while refreshing download authorization"
                )
            if response.status_code >= 400:
                raise PsnError(
                    f"download authorization refresh failed with HTTP {response.status_code}"
                )
            self._merge_cloudfront_cookies(response)
            if self._cookies_generation == seen_generation:
                log.warning("download authorization refresh returned no CloudFront cookies")

    def download_to(self, capture: Capture, dest: Path) -> tuple[str, int]:
        field, url = self._download_source(capture)
        if not url:
            raise PsnError(f"no downloadable URL for capture {capture.id}")
        with self._cookie_lock:
            generation = self._cookies_generation
        diagnostic = ""
        # shortcut: every 403 costs one gallery refresh plus a retry. If the
        # new logs show persistent S3 AccessDenied (missing media), skip the
        # retry for that case instead of re-fetching authorization.
        for attempt in (1, 2):
            headers: dict[str, str] = {}
            cookies = self.cloudfront_cookies
            if cookies:
                headers["Cookie"] = cookies
            with self._client.stream("GET", url, headers=headers) as response:
                if response.status_code < 400:
                    return write_atomic(dest, response.iter_bytes(256 * 1024))
                diagnostic = self._describe_failure(response, capture, field, url)
                if response.status_code != 403 or attempt == 2:
                    raise PsnError(diagnostic)
            try:
                self._refresh_download_authorization(generation)
            except PsnError as exc:
                raise PsnError(f"{diagnostic}; authorization refresh failed: {exc}") from exc
        raise PsnError(diagnostic)
