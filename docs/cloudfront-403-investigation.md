# Intermittent CloudFront 403s on capture downloads

Investigation for issue #6. This documents what the code does, the live
reproduction that confirmed the cause, and the diagnostics added.

## What the API returns

Verified against a real PlayStation App gallery response (captured March 2026,
posted in andshrew/PlayStation-Trophies#42):

- Images carry `screenshotUrl`; videos carry `videoUrl` (HLS `master_playlist.m3u8`)
  and `downloadUrl` (progressive `master_playlist.mp4`). The URLs have no query
  strings; media paths are `https://d1jbbh20jxa2st.cloudfront.net/<capture-id>/<file>`.
- The listing response sets three cookies: `CloudFront-Policy`,
  `CloudFront-Signature`, `CloudFront-Key-Pair-Id`, with
  `Domain=d1jbbh20jxa2st.cloudfront.net; Path=/`. They are the only authorization
  material sent to the media URLs. Cookie scope is the distribution, not an
  individual capture or gallery page.
- Items also expose `transcodeStatus`, `transcodeProgress`, `transcodeError`,
  `cloudStatus`, `sourceOfMedia`, and `expireAt`. A video whose transcode did not
  finish can still have a `downloadUrl` that has no object behind it.

The existing field selection (`download_url`, falling back to `video_url` for
videos; `screenshot_url` for images) matches the observed response and is kept.

## Root cause status: confirmed

Live run on 2026-11-23 with a valid NPSSO (34 listed): 26 downloaded, 8 failed
with HTTP 403. Every failure had:

```
transcode=ready_to_transcode source=s3
cookies=CloudFront-Key-Pair-Id,CloudFront-Policy,CloudFront-Signature
x-cache=Error from cloudfront
body=<Error><Code>AccessDenied</Code></Error>
```

The failures were exactly the 8 newest captures (uploaded 2026-11-22 to
2026-11-23); the newest successful download was uploaded 2026-11-21 23:45.
All three CloudFront cookies were present and identical across successful and
failing requests, so this is **not** expired/invalid authorization and not
concurrency.

Cause: PSN lists a video before its transcode has produced the MP4. The
`downloadUrl` exists but the object does not, so the S3 origin answers
`AccessDenied` and CloudFront turns it into 403. The capture becomes
downloadable once `transcodeStatus` reaches `complete`.

The earlier "intermittent" pattern is explained: each run downloaded the
captures that had finished transcoding and failed on the newest ones, which
stay in `ready_to_transcode` for a while.

The refresh-and-retry added for the cookie-expiry theory does not help these
requests (there is nothing to authorize); it remains as a safeguard for genuine
authorization expiry, and `sync` now skips non-complete videos entirely.

Cookie handling had one real defect regardless of cause: each gallery page
replaced the whole cookie set with whatever that page returned. If a page ever
returns a subset, the other cookies were dropped. Cookies are now merged by
name and the whole set is preserved.

## Changes

- `sync` leaves a video out of the run until `transcodeStatus` is `complete`
  (or absent), logging it as not ready and retrying on the next poll instead of
  counting a failure. This is the fix for the observed 403s.
- `download_to()` retries a 403 once after re-listing the gallery (limit 1) to
  refresh the CloudFront cookies. Concurrent 403s share one refresh. Other
  status codes are not retried.
- Failed downloads now log a redacted diagnostic line: status, capture ID,
  selected field, hostname, path shape, cookie *names* present, transcode status
  and source, `x-amz-cf-id`, `x-cache`, `via`, `content-type`, and a 300-character
  sanitized body excerpt.
- Never logged: cookie values, request query strings, full media URLs, bearer
  tokens, or NPSSO material. URLs inside error bodies are replaced with `<url>`.

## How to read the next failure

- Body `AccessDenied` / `NoSuchKey` with `x-cache: Error from cloudfront`:
  the object is missing or not ready. Check `transcode=` in the same line; a
  non-`complete` status or a non-empty `transcodeError` confirms it. Those
  captures are genuinely unavailable to the tool, not an auth failure.
- Body mentioning `expired`, `signature`, or `Policy`: the refreshed
  authorization was still rejected; capture the `x-amz-cf-id` and the
  `CloudFront-Policy` expiry from a manual listing to prove expiry.
- A 403 that succeeds on the immediate retry confirms the stale-authorization
  cause; the failed-attempt diagnostic is only logged when the retry also fails,
  so check whether the capture later downloads.

## Tests

`tests/test_psn.py` covers: all three cookies extracted from separate headers,
merging cookies across pages without dropping names, 403-triggered refresh and
retry, non-403 errors not retried, URL selection and video HLS fallback, and
redaction of signed query strings/URLs in failure diagnostics.
`tests/test_sync.py` covers skipping a `ready_to_transcode` video and
downloading a `complete` one.
