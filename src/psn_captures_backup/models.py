from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

UGC_IMAGE = 1
UGC_VIDEO = 2


class CaptureParseError(ValueError):
    """Raised when a ugcDocument entry is missing required fields."""


def _first_nonempty_string(item: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CaptureParseError(f"expected ISO-8601 string, got {type(value).__name__}")
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class Capture:
    id: str
    ugc_type: int
    game_title: str
    upload_date: datetime
    title_id: str | None = None
    screenshot_url: str | None = None
    download_url: str | None = None
    video_url: str | None = None
    expire_at: datetime | None = None
    resolution: str | None = None
    file_size: int | None = None
    file_type: str | None = None
    source_of_media: str | None = None

    @property
    def is_image(self) -> bool:
        return self.ugc_type == UGC_IMAGE

    @property
    def is_video(self) -> bool:
        return self.ugc_type == UGC_VIDEO

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> Capture:
        if "id" not in item:
            raise CaptureParseError("missing required field: 'id'")
        if "ugcType" not in item:
            raise CaptureParseError("missing required field: 'ugcType'")
        capture_id = item["id"]
        ugc_type = item["ugcType"]
        if not isinstance(capture_id, str) or not capture_id:
            raise CaptureParseError("capture 'id' must be a non-empty string")
        if ugc_type not in (UGC_IMAGE, UGC_VIDEO):
            raise CaptureParseError(f"unsupported ugcType: {ugc_type!r}")
        upload_date = _parse_datetime(item.get("uploadDate"))
        if upload_date is None:
            raise CaptureParseError("'uploadDate' is required")
        title = item.get("sceTitleName")
        if not isinstance(title, str) or not title.strip():
            title = "Unknown Game"
        return cls(
            id=capture_id,
            ugc_type=ugc_type,
            game_title=title,
            title_id=item.get("sceTitleId") if isinstance(item.get("sceTitleId"), str) else None,
            upload_date=upload_date.astimezone(UTC),
            screenshot_url=item.get("screenshotUrl"),
            download_url=item.get("downloadUrl"),
            video_url=item.get("videoUrl"),
            expire_at=_parse_datetime(item.get("expireAt")),
            resolution=item.get("resolution"),
            file_size=item.get("fileSize"),
            file_type=item.get("fileType"),
            source_of_media=item.get("sourceOfMedia"),
        )
