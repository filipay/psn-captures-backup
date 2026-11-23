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
    "transcodeStatus": "complete",
    "sourceOfMedia": "s3",
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


def test_video_falls_back_to_hls_url() -> None:
    requested = {}
    video = {key: value for key, value in VIDEO.items() if key != "downloadUrl"}
    video["videoUrl"] = "https://cdn.example/clip/master_playlist.m3u8"

    def handler(request: httpx.Request) -> httpx.Response:
        requested["url"] = str(request.url)
        return httpx.Response(200, content=b"v")

    client = PsnClient("TOKEN", client=_client(handler))
    capture = Capture.from_api(video)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        client.download_to(capture, Path(tmp) / "out.mp4")
    assert requested["url"] == "https://cdn.example/clip/master_playlist.m3u8"
    client.close()


def test_extracts_all_cloudfront_cookies_from_separate_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ugcDocument": [], "nextCursorMark": "-1"},
            headers=[
                ("set-cookie", "CloudFront-Policy=policy; Domain=cdn.example; Path=/; Secure"),
                ("set-cookie", "CloudFront-Signature=sig; Domain=cdn.example; Path=/; Secure"),
                ("set-cookie", "CloudFront-Key-Pair-Id=pair; Domain=cdn.example; Path=/; Secure"),
                ("set-cookie", "session=secret; Path=/"),
            ],
        )

    client = PsnClient("TOKEN", client=_client(handler))
    client.list_captures()
    cookies = client.cloudfront_cookies
    assert "CloudFront-Policy=policy" in cookies
    assert "CloudFront-Signature=sig" in cookies
    assert "CloudFront-Key-Pair-Id=pair" in cookies
    assert "session=" not in cookies
    client.close()


def test_cookie_refresh_keeps_cookies_absent_from_newer_page() -> None:
    pages = iter(
        [
            {
                "ugcDocument": [],
                "nextCursorMark": "PAGE2",
                "limit": 1,
            },
            {
                "ugcDocument": [],
                "nextCursorMark": "-1",
                "limit": 1,
            },
        ]
    )
    first = True

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal first
        if first:
            first = False
            headers = [
                ("set-cookie", "CloudFront-Policy=old; Path=/"),
                ("set-cookie", "CloudFront-Signature=sig; Path=/"),
                ("set-cookie", "CloudFront-Key-Pair-Id=pair; Path=/"),
            ]
        else:
            headers = [("set-cookie", "CloudFront-Policy=new; Path=/")]
        return httpx.Response(200, json=next(pages), headers=headers)

    client = PsnClient("TOKEN", client=_client(handler))
    client.list_captures()
    cookies = client.cloudfront_cookies
    assert "CloudFront-Policy=new" in cookies
    assert "CloudFront-Signature=sig" in cookies
    assert "CloudFront-Key-Pair-Id=pair" in cookies
    client.close()


def test_download_403_refreshes_authorization_and_retries() -> None:
    state = {"downloads": 0, "gallery": 0}
    seen_cookies = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "cloudMediaGallery" in str(request.url):
            state["gallery"] += 1
            return httpx.Response(
                200,
                json={"ugcDocument": [], "nextCursorMark": "-1"},
                headers={"set-cookie": "CloudFront-Policy=fresh; Path=/"},
            )
        state["downloads"] += 1
        seen_cookies.append(request.headers.get("cookie"))
        if state["downloads"] == 1:
            return httpx.Response(403, content=b"denied")
        return httpx.Response(200, content=b"video")

    client = PsnClient("TOKEN", client=_client(handler))
    client.cloudfront_cookies = "CloudFront-Policy=old"
    capture = Capture.from_api(VIDEO)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "out.mp4"
        client.download_to(capture, dest)
        assert dest.read_bytes() == b"video"
    assert seen_cookies[0] == "CloudFront-Policy=old"
    assert seen_cookies[1] == "CloudFront-Policy=fresh"
    assert state["gallery"] == 1
    client.close()


def test_persistent_403_reports_redacted_diagnostics() -> None:
    video = dict(VIDEO)
    video["downloadUrl"] = (
        "https://cdn.example/psn2/master_playlist.mp4"
        "?Signature=SECRET&Key-Pair-Id=SECRET2"
    )
    state = {"downloads": 0, "gallery": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "cloudMediaGallery" in str(request.url):
            state["gallery"] += 1
            return httpx.Response(
                200,
                json={"ugcDocument": [], "nextCursorMark": "-1"},
                headers={"set-cookie": "CloudFront-Policy=fresh; Path=/"},
            )
        state["downloads"] += 1
        return httpx.Response(
            403,
            headers={"x-amz-cf-id": "req-123", "x-cache": "Error from cloudfront"},
            content=(
                b"<Error><Code>AccessDenied</Code>"
                b"<Message>cannot fetch https://secret.example/object?token=SECRET</Message>"
                b"</Error>"
            ),
        )

    client = PsnClient("TOKEN", client=_client(handler))
    client.cloudfront_cookies = "CloudFront-Policy=old"
    capture = Capture.from_api(video)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp, pytest.raises(PsnError) as info:
        client.download_to(capture, Path(tmp) / "out.mp4")

    message = str(info.value)
    assert "HTTP 403 for capture psn2" in message
    assert "field=download_url" in message
    assert "host=cdn.example" in message
    assert "path=/psn2/master_playlist.mp4" in message
    assert "cookies=CloudFront-Policy" in message
    assert "transcode=complete" in message
    assert "source=s3" in message
    assert "x-amz-cf-id=req-123" in message
    assert "x-cache=Error from cloudfront" in message
    assert "AccessDenied" in message
    assert "SECRET" not in message
    assert "secret.example" not in message
    assert state["downloads"] == 2
    assert state["gallery"] == 1
    client.close()


def test_non_403_error_is_not_retried() -> None:
    state = {"downloads": 0, "gallery": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "cloudMediaGallery" in str(request.url):
            state["gallery"] += 1
            return httpx.Response(200, json={"ugcDocument": [], "nextCursorMark": "-1"})
        state["downloads"] += 1
        return httpx.Response(404, content=b"missing")

    client = PsnClient("TOKEN", client=_client(handler))
    capture = Capture.from_api(VIDEO)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp, pytest.raises(PsnError):
        client.download_to(capture, Path(tmp) / "out.mp4")
    assert state == {"downloads": 1, "gallery": 0}
    client.close()
