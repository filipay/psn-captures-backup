from datetime import UTC, datetime

import pytest

from psn_captures_backup.models import Capture, CaptureParseError

IMAGE_ITEM = {
    "id": "psn88a578fc0d3848b1a41142f75a3d62ad",
    "ugcType": 1,
    "sceTitleName": "Marvel’s Spider-Man 2",
    "uploadDate": "2025-10-11T23:06:27.983Z",
    "expireAt": "2025-10-25T23:06:27.983Z",
    "screenshotUrl": "https://d1jbbh20jxa2st.cloudfront.net/psn88.../screenshotUrl.jpg",
    "resolution": "3840x2160",
    "fileSize": 115163712,
    "fileType": "JPEG",
    "sourceOfMedia": "s3",
}

VIDEO_ITEM = {
    "id": "psn35eb8b82b1b14b19a755fe646d1fb94f",
    "ugcType": 2,
    "sceTitleName": "Marvel’s Spider-Man 2",
    "uploadDate": "2025-10-11T23:06:27.983Z",
    "videoUrl": "https://d1jbbh20jxa2st.cloudfront.net/psn35.../master_playlist.m3u8",
    "downloadUrl": "https://d1jbbh20jxa2st.cloudfront.net/psn35.../master_playlist.mp4",
    "fileType": "WEBM",
    "sourceOfMedia": "s3",
}


def test_parse_image() -> None:
    capture = Capture.from_api(IMAGE_ITEM)
    assert capture.id == IMAGE_ITEM["id"]
    assert capture.is_image and not capture.is_video
    assert capture.game_title == "Marvel’s Spider-Man 2"
    assert capture.upload_date == datetime(2025, 10, 11, 23, 6, 27, 983000, tzinfo=UTC)
    assert capture.file_size == 115163712


def test_parse_video() -> None:
    capture = Capture.from_api(VIDEO_ITEM)
    assert capture.is_video
    assert capture.download_url.endswith(".mp4")


def test_missing_id_raises() -> None:
    with pytest.raises(CaptureParseError, match="id"):
        Capture.from_api({"ugcType": 1, "uploadDate": "2025-10-11T23:06:27.983Z"})


def test_unknown_ugc_type_raises() -> None:
    with pytest.raises(CaptureParseError, match="ugcType"):
        Capture.from_api({"id": "x", "ugcType": 9, "uploadDate": "2025-10-11T23:06:27.983Z"})


def test_missing_title_falls_back() -> None:
    capture = Capture.from_api(
        {"id": "x", "ugcType": 1, "sceTitleName": None, "uploadDate": "2025-10-11T23:06:27.983Z"}
    )
    assert capture.game_title == "Unknown Game"
