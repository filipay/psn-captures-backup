from pathlib import Path

import httpx
import pytest

from psn_captures_backup.models import Capture
from psn_captures_backup.psn import PsnClient, PsnError

IMAGE = {
    "id": "psn1",
    "ugcType": 1,
    "sceTitleName": "Game A",
    "uploadDate": "2025-10-11T00:00:00Z",
    "screenshotUrl": "https://cdn.example/screenshot.jpg",
    "fileType": "JPEG",
}
VALHEIM = {
    "id": "psn3",
    "ugcType": 1,
    "sceTitleName": None,
    "sceTitleId": "PPSA28824_00",
    "title": "Valheim_20260926235351",
    "uploadDate": "2025-10-13T00:00:00Z",
    "screenshotUrl": "https://cdn.example/valheim.jpg",
    "fileType": "JPEG",
}
UNKNOWN_GAME = {
    "id": "psn4",
    "ugcType": 1,
    "sceTitleName": None,
    "sceTitleId": "CUSA00000_00",
    "sceUserAccountId": "1234567890",
    "uploadDate": "2025-10-14T00:00:00Z",
    "screenshotUrl": "https://cdn.example/unknown.jpg",
    "fileType": "JPEG",
}

VIDEO = {
    "id": "psn2",
    "ugcType": 2,
    "sceTitleName": "Game A",
    "uploadDate": "2025-10-12T00:00:00Z",
    "downloadUrl": "https://cdn.example/clip.mp4",
    "fileType": "WEBM",
}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_list_captures_follows_cursor_and_captures_cookies() -> None:
    pages = [
        {
            "ugcDocument": [IMAGE],
            "nextCursorMark": "PAGE2",
            "limit": 1,
        },
        {
            "ugcDocument": [VIDEO, VALHEIM, UNKNOWN_GAME],
            "nextCursorMark": "-1",
            "limit": 1,
        },
    ]
    seen_params = []
    seen_lookup_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "gamelist/v2/users" in url:
            seen_lookup_urls.append(url)
            return httpx.Response(
                200,
                json={"name": "Fallback Game", "localizedName": "Fallback Game (EN)"},
            )
        if "cdn.example" in url:
            return httpx.Response(200, content=b"media")
        seen_params.append(dict(request.url.params))
        headers = {
            "set-cookie": "CloudFront-Policy=abc; Path=/; Secure",
        }
        if len(seen_params) == 1:
            headers["set-cookie"] = "CloudFront-Policy=abc; Path=/"
        return httpx.Response(200, json=pages[len(seen_params) - 1], headers=headers)

    client = PsnClient("TOKEN", client=_client(handler))
    captures = client.list_captures()
    assert [c.id for c in captures] == ["psn1", "psn2", "psn3", "psn4"]
    assert captures[2].game_title == "Valheim"
    assert captures[2].title_id == "PPSA28824_00"
    assert captures[3].game_title == "Fallback Game (EN)"
    assert seen_lookup_urls == [
        "https://m.np.playstation.net/api/gamelist/v2/users/1234567890/titles/CUSA00000_00"
    ]
    assert seen_params[0]["includeTokenizedUrls"] == "true"
    assert seen_params[1]["nextCursorMark"] == "PAGE2"
    assert client.cloudfront_cookies == "CloudFront-Policy=abc"
    client.close()


def test_download_to_writes_file_with_cookie_header() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie")
        return httpx.Response(200, content=b"binary-data")

    client = PsnClient("TOKEN", client=_client(handler))
    client.cloudfront_cookies = "CloudFront-Policy=abc"
    capture = Capture.from_api(IMAGE)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "out.jpg"
        _sha256, size = client.download_to(capture, dest)
        assert dest.read_bytes() == b"binary-data"
        assert size == 11
        assert seen["cookie"] == "CloudFront-Policy=abc"
    client.close()


def test_download_http_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"denied")

    client = PsnClient("TOKEN", client=_client(handler))
    capture = Capture.from_api(IMAGE)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp, pytest.raises(PsnError):
        client.download_to(capture, Path(tmp) / "out.jpg")
    client.close()


def test_video_uses_download_url_over_hls() -> None:
    requested = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requested["url"] = str(request.url)
        return httpx.Response(200, content=b"v")

    client = PsnClient("TOKEN", client=_client(handler))
    capture = Capture.from_api(VIDEO)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        client.download_to(capture, Path(tmp) / "out.mp4")
    assert requested["url"] == "https://cdn.example/clip.mp4"
    client.close()
