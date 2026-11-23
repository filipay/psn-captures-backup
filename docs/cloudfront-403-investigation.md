# Intermittent CloudFront 403s on capture downloads

Investigation for issue #6. This documents what the code does, what was
verified without a live account, the remaining hypotheses, and the diagnostics
added so the next real run can tell the causes apart.

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

## Root cause status

Not confirmed: this checkout's NPSSO is expired, so the failing sync could not be
reproduced live. The issue evidence narrows it to two causes:

1. **Download authorization expires during a long sync.** The cookies come from
   the initial listing; a sync of many large videos can outlive them. Requests
   processed later then get 403 while earlier ones succeed — matching
   "some 200, some 403 on the same hostname in one run" and the handful of
   successes per run.
2. **Per-capture media is missing or not ready.** CloudFront fronts an S3 origin,
   and S3 answers a missing key with `AccessDenied` when listing is not granted,
   which CloudFront passes through as 403. A non-complete `transcodeStatus` or
   `transcodeError` would explain a capture that always fails.

Concurrency is not a plausible cause: all workers send the same distribution
cookies, so one capture succeeding proves the authorization path works; a
concurrent request cannot invalidate it. The default concurrency is unchanged.

Cookie handling had one real defect regardless of cause: each gallery page
replaced the whole cookie set with whatever that page returned. If a page ever
returns a subset, the other cookies were dropped. Cookies are now merged by
name and the whole set is preserved.

## Changes

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
