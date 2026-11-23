# psn-captures-backup

Back up PlayStation captures (screenshots and short gameplay clips) from PSN to
local storage before Sony deletes them from the cloud.

PlayStation auto-uploads captures to PSN where they show up in the PlayStation
App's **Captures** tab. Sony deletes them from the cloud **after about 14 days**
(the API response exposes an `expireAt` timestamp). There is no official API,
export endpoint, or supported way to back them up. This tool polls PSN
low-frequency, downloads new captures to a predictable folder layout, dedupes
via a local SQLite state file, and never re-fetches a capture it already has.

The **download is the product**: the output folder is backend-agnostic, so any
downstream tool (immich-go, rclone, Jellyfin, a plain NAS) can consume it. See
[the Immich recipe](docs/immich.md).

## Why 14 days matters

Sony's cloud copies of console auto-uploads are retention-limited: **captures
disappear about 14 days after upload**. Once deleted, they are gone from PSN —
the only remaining copy is whatever lives on the console (or a manual export).
The default `daemon` poll interval is 6 hours, comfortably inside that window,
so a capture is backed up long before expiry even if the machine was offline for
a day.

## Install

### Docker

A prebuilt image is published to GitHub Container Registry (GHCR). From the
repository root:

```bash
cp .env.example .env
# edit .env and set NPSSO (see below)
docker pull ghcr.io/filipay/psn-captures-backup:latest

docker run -d --name psn-captures-backup \
  --env-file .env \
  -v /path/to/psn-captures:/captures \
  -e PSN_OUTPUT_DIR=/captures \
  -e PSN_STATE_FILE=/captures/.psn-captures-state.sqlite \
  -e PSN_TOKEN_FILE=/captures/.psn-token.json \
  psn-captures-backup:latest daemon
```

Or edit the example compose file (set the bind-mount path and the `user:`) and
let Compose pull the published image:

```bash
cp .env.example .env
docker compose -f docker-compose.example.yml up -d
```

Replace `/path/to/psn-captures` with the host folder captures should be written
to (the container sees it as `/captures`).

`docker-compose.example.yml` pulls the published GHCR image. A local
`docker-compose.yml` is gitignored for instance-specific settings; if you keep
one, expect to build/tag `psn-captures-backup:latest` yourself and adjust its
hard-coded mount path.

The image is multi-stage and runs as a non-root user (uid `10001` by default).
It creates `/captures` owned by that user, so a **named volume** just works. With
a **bind mount** to a host path, the host directory's ownership applies instead,
and the container fails with `unable to open database file` if it cannot write
there. Either:

- run the container as the user that owns the folder — set `user: "<uid>:<gid>"`
  (find yours with `id -u` / `id -g`); the example compose file does this, or
- give the folder to uid `10001`: `sudo chown -R 10001:10001 /path/to/psn-captures`.

### pip

Requires Python >= 3.11.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
# or, for development:
.venv/bin/python -m pip install -e ".[dev]"
```

Then run the CLI:

```bash
psn-captures-backup --help
```

## Getting your NPSSO

`NPSSO` is the Sony cookie the PlayStation App uses to authenticate. Treat it
like a password.

1. Log in to <https://www.playstation.com> in a desktop browser.
2. Visit <https://ca.account.sony.com/api/v1/ssocookie> in the same browser.
3. Copy the **64-character** `npsso` value from the JSON response.
4. Put it in your `.env` (or environment) as `NPSSO=...`.

The NPSSO itself expires after roughly **60 days**. When it does, `auth`, `list`
and `sync` log a clear error and exit `1`; `daemon` logs the error, reports
`psn_auth_ok 0`, and keeps polling, but configuration is read at startup, so it
only picks up a new NPSSO after a restart.

After adding or changing `NPSSO`, run `auth` once to verify it and write the
token file:

```bash
psn-captures-backup auth
# In Docker, run the same one-off container as `daemon` with this command instead.
```

That gives immediate feedback instead of waiting for the first scheduled sync,
and leaves `daemon` with a fresh token file to reuse and refresh.

## Token file (`PSN_TOKEN_FILE`)

You do not create this file by hand. Run `psn-captures-backup auth` once after
setting `NPSSO`: it exchanges the NPSSO for an access/refresh token pair and
writes it to `PSN_TOKEN_FILE` — by default `<PSN_OUTPUT_DIR>/.psn-token.json`,
mode `0600`, JSON. `auth --check` validates an existing file instead of printing
the token length. `list`, `sync` and `daemon` also create the file if it is
missing, but running `auth` first surfaces credential problems immediately.
Later runs reuse the file and refresh the short-lived access token in place.

```bash
psn-captures-backup auth                                  # create or update the token file
psn-captures-backup auth --check                          # validate; refreshes if needed
psn-captures-backup auth --token-file /captures/.psn-token.json
```

- The file holds secrets (`access_token`, `refresh_token`). Keep it on
  persistent, private storage; never commit or share it.
- In Docker, point `PSN_TOKEN_FILE` at the mounted volume (the examples use
  `/captures/.psn-token.json`) so tokens survive container restarts.
- If the file is missing or unreadable it is recreated from `NPSSO`. If a
  refresh is rejected the tool falls back to a fresh `NPSSO` exchange.
- The refresh token lives about **10 days** but is rotated on every refresh, so
  a running daemon keeps it alive indefinitely. If the container is stopped for
  longer than that, the next run needs a still-valid NPSSO. Run a sync (or the
  daemon) at least every few days, or alert on the metric below.
- `NPSSO` must still be set even when a valid token file exists; it is the
  fallback when the refresh token expires. Delete the file to force a fresh
  exchange, for example after changing accounts.

## Configuration

Environment variables (also readable from a `.env` file):

| Variable | Default | Meaning |
|---|---|---|
| `NPSSO` | — (required) | Sony NPSSO cookie value. Secret. |
| `PSN_OUTPUT_DIR` | `./captures` | Where downloaded media is written. |
| `PSN_STATE_FILE` | `<output>/.psn-captures-state.sqlite` | State DB path. |
| `PSN_TOKEN_FILE` | `<output>/.psn-token.json` | Persisted access/refresh tokens. Created automatically by `auth` (or the first sync); secret, written `0600`. |
| `PSN_POLL_INTERVAL` | `21600` (6h) | Seconds between `daemon` syncs. |
| `PSN_MAX_CONCURRENCY` | `2` | Parallel downloads. |
| `PSN_UPLOAD_CONCURRENCY` | `2` | Maximum number of post-download hooks running at once. |
| `PSN_UPLOAD_QUEUE_LIMIT` | `4` | Maximum number of queued or running hook batches; downloads wait for capacity. |
| `PSN_POST_DOWNLOAD_DEBOUNCE_SECONDS` | `0` | Quiet period before post-download hooks run as one batch; `0` preserves per-download hooks. |
| `PSN_INCLUDE_IMAGES` | `true` | Download screenshots. |
| `PSN_INCLUDE_VIDEOS` | `true` | Download video clips. |
| `PSN_LOG_LEVEL` | `INFO` | Log verbosity. |
| `PSN_METRICS_ADDR` | — (disabled) | Serve Prometheus metrics on `HOST:PORT` from `daemon`. |
| `PSN_POST_DOWNLOAD_SCRIPT` | — | Optional hook run after successful downloads. The hook receives one or more paths when debounce is enabled. |

Precedence is CLI flag > environment/`.env` > built-in default.

## CLI usage

```
psn-captures-backup sync      # one-shot: list, download new, update state (cron-friendly)
psn-captures-backup daemon    # run sync every PSN_POLL_INTERVAL (default 6h)
psn-captures-backup list      # print available captures (no download); debug/verification
psn-captures-backup auth      # bootstrap NPSSO -> persist refresh token; --check validates
```

Per-command flags override the environment:

- `--output-dir PATH`
- `--state-file PATH`
- `--token-file PATH`
- `--no-images` / `--no-videos`
- `--max-concurrency N`
- `--upload-concurrency N`
- `--upload-queue-limit N`
- `--dry-run` (`sync`)
- `--post-download-script PATH`
- `--post-download-debounce-seconds N`
- `--redownload` (`sync`; re-fetches captures already recorded in state)
- `--flat`
- `--json-log`
- `--verbose`
- `--interval N` (`daemon`)

Exit codes: `0` success; `1` auth/PSN/runtime failure, or a `sync` where any
download failed; `2` configuration error. `daemon` keeps running through failed
cycles and returns `0` on a clean shutdown.

Typical first run:

```bash
psn-captures-backup auth --check   # validate NPSSO; create/refresh the token file
psn-captures-backup list           # see what is available
psn-captures-backup sync           # download everything new once
psn-captures-backup daemon         # keep it running
```

`auth` (with or without `--check`) is the recommended first step: it writes
`PSN_TOKEN_FILE` immediately, so the daemon reuses a known-good token instead of
discovering a bad NPSSO on its first cycle.

### Reset and force re-download

- `sync --redownload` re-fetches every capture recorded in state (still limited
  to PSN's ~14-day retention).
- Deleting the state database forgets all history, so everything currently in
  PSN is treated as new. It does not delete any media.
- Deleting the token file forces a fresh NPSSO exchange; run `auth` afterwards.

## Metrics

Set `PSN_METRICS_ADDR` and publish the port to serve Prometheus metrics from
`daemon` (disabled by default):

```bash
docker run ... -e PSN_METRICS_ADDR=0.0.0.0:9101 -p 9101:9101 psn-captures-backup:latest daemon
```

Grafana Alloy:

```alloy
prometheus.scrape "psn_captures" {
  targets      = [{ __address__ = "psn-captures-backup:9101" }]
  metrics_path = "/metrics"
  forward_to   = [prometheus.remote_write.default.receiver]
}
```

Metrics:

| Metric | Meaning |
|---|---|
| `psn_auth_ok` | `1` when the last sync authenticated. |
| `psn_auth_last_success_timestamp_seconds` | Last successful PSN auth. |
| `psn_sync_last_success_timestamp_seconds` | Last successful sync. |
| `psn_sync_captures_not_ready` | Videos skipped because their transcode is not complete. |
| `psn_sync_failures_total` | Download failures since process start. |
| `psn_token_file_present` | `1` if the token file is readable. |
| `psn_token_access_expires_timestamp_seconds` | Access token expiry (epoch). |
| `psn_token_refresh_expires_timestamp_seconds` | Refresh token expiry (epoch). |

Example alert conditions:

- Refresh token about to lapse / daemon stopped:
  `time() > psn_token_refresh_expires_timestamp_seconds - 2 * 86400`
- Auth broken: `psn_auth_ok == 0`
- No successful sync in 2 days:
  `time() - psn_sync_last_success_timestamp_seconds > 2 * 86400`
- Captures stuck not ready: `psn_sync_captures_not_ready > 0` for 24h
- New download failures: `increase(psn_sync_failures_total[1h]) > 0`

The daemon itself logs a warning when the refresh token has less than **3 days**
left; the example alert above fires sooner, at 2 days.

The endpoint exposes only timestamps and counts — no tokens, cookies or URLs.
It is served by `daemon`; one-shot `sync` exits before it can be scraped.

## Output layout

By default each game gets its own folder:

```
<output-dir>/<Sanitized Game Title>/<YYYY-MM-DD>_<capture-id>.<ext>

When the PSN capture title contains an exact `YYYYMMDDHHMMSS` suffix, the full
capture timestamp is used instead:
<output-dir>/<Sanitized Game Title>/<YYYY-MM-DD>_<HH-MM-SS>_<capture-id>.<ext>
```

For example:

```
captures/Example Game/2025-10-11_14-30-25_psn88a578fc0d3848b1a41142f75a3d62ad.jpg
```

- When PSN's capture title has an exact 14-digit timestamp (for example
  `Valheim_20260926235351`), that timestamp is used in the filename as
  `YYYY-MM-DD_HH-MM-SS`. Otherwise the filename uses the capture's PSN
  `uploadDate` date (UTC).
- Extensions are chosen from the file type: `jpg`/`png` for images, `mp4` for
  videos. Video captures use the direct MP4 `downloadUrl`, not the HLS playlist.
- Game titles are sanitized for filesystem safety (path separators, control
  characters, and trailing dots/spaces are replaced or trimmed). The unique
  capture id in the filename disambiguates any colliding titles.
- `--flat` writes everything directly under `<output-dir>`. That folder also
  holds the state database and token file by default, so point downstream tools
  (immich-go, rclone, Jellyfin, a NAS indexer) at it only after moving both
  elsewhere with `PSN_STATE_FILE` and `PSN_TOKEN_FILE` — or confirm the consumer
  skips dotfiles. The token file is a secret.
- Writes are atomic: media goes to a temporary `.part` file in the destination
  directory, is `fsync`ed, then renamed into place. Partial downloads are never
  left behind.
- A capture recorded in state is skipped on later runs even if its local file
  was deleted, so external cleanup (or a post-download hook) cannot trigger a
  re-download. Pass `--redownload` to `sync` to fetch recorded captures again.

## Post-download hook

Set `PSN_POST_DOWNLOAD_SCRIPT=/path/to/hook.sh` (or `--post-download-script`) to
run an executable after successful downloads. By default the hook runs once per
download. Set `PSN_POST_DOWNLOAD_DEBOUNCE_SECONDS` (or
`--post-download-debounce-seconds`) to a positive value to collect captures
until that many seconds have passed without another successful download, then
invoke the hook once with all captured paths:

```bash
/path/to/hook.sh /absolute/path/to/file-a.jpg /absolute/path/to/file-b.mp4
```

This is useful for downstream tools that scan a directory: a burst of captures
can trigger one scan instead of one scan per file. The default `0` keeps the
existing one-hook-per-download behavior.

With a positive debounce, hook batches run concurrently in a separate worker
pool after state rows are written, so a slow hook does not block downloads.
With the default debounce `0`, the hook is submitted inline and the download
loop waits when `PSN_UPLOAD_QUEUE_LIMIT` batches are queued or running.
`PSN_UPLOAD_CONCURRENCY` controls how many hook batches run at once, while
`PSN_UPLOAD_QUEUE_LIMIT` bounds queued plus running hook batches. The sync
command waits for all submitted hooks before exiting.

Each hook invocation times out after **300 seconds**. Hook failures are logged
and do not abort the sync; a failed or timed-out hook is not retried. Only
captures whose *download* failed are marked `failed` and retried on the next
poll. Keep hooks under five minutes — for slower work, return quickly and
continue in the background.

The hook exit status is recorded as a signal from your downstream tool: `0`
marks the capture `processed` (the script succeeded), a nonzero exit or timeout
leaves it `downloaded`. This is informational and does not assume the hook
uploads anything; both states are skipped on later syncs because the state DB
is the source of truth. Deleting local files after your own upload check is
therefore safe — they will not be downloaded again unless you run
`sync --redownload`.

When PSN does not provide `sceTitleName`, the client first uses the capture's
`title` field. For capture titles ending in an exact 14-digit timestamp, that
suffix is removed (for example, `Valheim_20260926235351` becomes `Valheim`).
If neither title field provides a usable name, the client uses
`sceUserAccountId` and `sceTitleId` with PSN's authenticated game-list
endpoint to resolve the title. Title lookups are cached for the duration of the
sync, and failures safely fall back to `Unknown Game`.

## Immich integration

The recommended way to get captures into Immich is to point **immich-go** at the
output folder. Because the game name lives only in the folder path, the recipe
invokes immich-go once per game folder and sets an explicit `--tag` to the folder
name (the built-in `--folder-as-tags` tags with a root-prefixed path such as
`psn-captures/Example Game`, not the bare game name).

See [docs/immich.md](docs/immich.md) for the full recipe: install/pin, config
file (API key off the command line), backlog upload, event-driven post-download
uploads, and the album-per-game alternative.

## Caveats

These are important. Read them before pointing this at your account.

1. **Unofficial API / ToS — account risk.** This uses reverse-engineered,
   undocumented endpoints that the PlayStation App itself uses. Sony's Terms of
   Service prohibit reverse engineering. Using this tool is **at your own risk**
   and could theoretically lead to account restrictions. It polls at a low
   frequency for this reason; do not crank the interval down.
2. **14-day retention.** PSN deletes cloud captures about 14 days after upload.
   If the tool is not running (or your NPSSO has expired) for longer than that,
   those captures are unrecoverable from the cloud.
3. **Limited auto-upload.** Only **screenshots** and gameplay videos **under
   3 minutes and at most 1080p** are auto-uploaded by the console. Full-quality
   captures require a manual console-side USB export; this tool cannot retrieve
   what the console never uploaded.
4. **Videos appear before their transcode finishes.** PSN lists a new video
   while `transcodeStatus` is still `ready_to_transcode`; its download URL
   returns CloudFront 403 until the MP4 exists. The tool skips those captures
   (logged as "not ready yet") and retries them on the next poll, so they are
   not counted as failures. See
   [docs/cloudfront-403-investigation.md](docs/cloudfront-403-investigation.md).
5. **NPSSO expiry (~60 days).** The NPSSO cookie is short-lived. You must
   periodically paste a fresh one. When it expires, one-shot commands exit `1`
   and `daemon` logs auth failures until restarted with the new value (run
   `auth` first to confirm it). This is unavoidable without a full 2FA login
   flow, which is out of scope.
6. **Breakage after PS App updates.** The client credentials and endpoint
   shapes could change at any PlayStation App release, which may break listing or
   downloads until this project is updated.
7. **State is the source of truth for downloads.** Once a capture is recorded,
   it is not fetched again even if its local file is deleted — use
   `sync --redownload` to force a fresh copy. Deleting the state database makes
   the tool treat every capture still in PSN's 14-day window as new.

## Troubleshooting

- **`unable to open database file`** — the container cannot write to the bind
  mount. Run as the folder's owner (`user: "<uid>:<gid>"`) or
  `chown -R 10001:10001 <folder>` (see Install).
- **`NPSSO was rejected or has expired`** — paste a fresh NPSSO, run
  `psn-captures-backup auth`, then restart `daemon` so it reads the new value.
  If `auth` still fails, the NPSSO itself is invalid.
- **`403` / `AccessDenied` / `NoSuchKey` for a video** — PSN listed the capture
  before its transcode finished, so the MP4 does not exist yet. It is skipped as
  "not ready" and retried on later polls; see
  [docs/cloudfront-403-investigation.md](docs/cloudfront-403-investigation.md).
  A capture that never becomes ready stays skipped and shows up only in
  `psn_sync_captures_not_ready`.
- **Hook exits nonzero or times out** — the file stays on disk with status
  `downloaded`; hooks are not retried. Check the hook's own log, and keep it
  under the 300-second limit.
- **`auth --check` passes but downloads fail** — listing and token exchange are
  fine; media authorization/availability is a separate path. Check the redacted
  `download failed` line, then the investigation guide above.
