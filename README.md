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

The image is multi-stage and runs as a non-root user (uid `10001` by default).
It creates `/captures` owned by that user, so a **named volume** just works. With
a **bind mount** to a host path, the host directory's ownership applies instead,
and the container fails with `unable to open database file` if it cannot write
there. Either:

- run the container as the user that owns the folder — set `user: "<uid>:<gid>"`
  (find yours with `id -u` / `id -g`); the example compose file does this, or
- give the folder to uid `10001`: `sudo chown -R 10001:10001 /path/to/psn-captures`.

### pip

Requires Python >= 3.11 (`python3`; the tool targets Python 3, not 2).

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

The NPSSO itself expires after roughly **60 days**; when it does, the tool logs
a clear error and exits non-zero. Re-paste a fresh value and restart the daemon.

## Configuration

Environment variables (also readable from a `.env` file):

| Variable | Default | Meaning |
|---|---|---|
| `NPSSO` | — (required) | Sony NPSSO cookie value. Secret. |
| `PSN_OUTPUT_DIR` | `./captures` | Where downloaded media is written. |
| `PSN_STATE_FILE` | `<output>/.psn-captures-state.sqlite` | State DB path. |
| `PSN_TOKEN_FILE` | `<output>/.psn-token.json` | Persisted refresh token (secret, written `0600`). |
| `PSN_POLL_INTERVAL` | `21600` (6h) | Seconds between `daemon` syncs. |
| `PSN_MAX_CONCURRENCY` | `2` | Parallel downloads. |
| `PSN_UPLOAD_CONCURRENCY` | `2` | Maximum number of post-download hooks running at once. |
| `PSN_UPLOAD_QUEUE_LIMIT` | `4` | Maximum number of queued or running hooks; downloads wait for capacity. |
| `PSN_INCLUDE_IMAGES` | `true` | Download screenshots. |
| `PSN_INCLUDE_VIDEOS` | `true` | Download video clips. |
| `PSN_LOG_LEVEL` | `INFO` | Log verbosity. |
| `PSN_POST_DOWNLOAD_SCRIPT` | — | Optional hook run after each successful download. |

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
- `--no-images` / `--no-videos` (`sync`, `list`)
- `--max-concurrency N`
- `--upload-concurrency N`
- `--upload-queue-limit N`
- `--dry-run` (`sync`)
- `--post-download-script PATH`
- `--flat`
- `--json-log`
- `--verbose`
- `--interval N` (`daemon`)

Exit codes: `0` success, `1` runtime/auth/PSN failure, `2` configuration error.

Typical first run:

```bash
psn-captures-backup auth --check   # confirm the NPSSO works
psn-captures-backup list           # see what is available
psn-captures-backup sync           # download everything new once
psn-captures-backup daemon         # keep it running
```

## Output layout

By default each game gets its own folder:

```
<output-dir>/<Sanitized Game Title>/<YYYY-MM-DD>_<capture-id>.<ext>
```

For example:

```
captures/Example Game/2025-10-11_psn88a578fc0d3848b1a41142f75a3d62ad.jpg
```

- `<YYYY-MM-DD>` comes from the capture's PSN `uploadDate` (UTC), so names are
  stable across re-runs.
- Extensions are chosen from the file type: `jpg`/`png` for images, `mp4` for
  videos. Video captures use the direct MP4 `downloadUrl`, not the HLS playlist.
- Game titles are sanitized for filesystem safety (path separators, control
  characters, and trailing dots/spaces are replaced or trimmed). The unique
  capture id in the filename disambiguates any colliding titles.
- `--flat` writes everything directly under `<output-dir>`.
- Writes are atomic: media goes to a temporary `.part` file in the destination
  directory, is `fsync`ed, then renamed into place. Partial downloads are never
  left behind.
- A capture already recorded in state **and** still present on disk is skipped,
  so re-runs are idempotent. If its local file went missing, it is re-downloaded.

## Post-download hook

Set `PSN_POST_DOWNLOAD_SCRIPT=/path/to/hook.sh` (or `--post-download-script`) to
run an executable after each successful download. The script is invoked as:

```bash
/path/to/hook.sh /absolute/path/to/downloaded/file
```

Hooks run concurrently in a separate worker pool after the state row is written,
so a slow upload does not prevent other captures from downloading.

When PSN does not provide `sceTitleName`, the client uses the capture's
`sceTitleId` to look up the game name from the PlayStation Store title metadata
endpoint. The lookup is cached for the duration of the sync; if it fails, the
capture remains under `Unknown Game` rather than failing the backup.
`PSN_UPLOAD_CONCURRENCY` controls how many hooks run at once, while
`PSN_UPLOAD_QUEUE_LIMIT` bounds queued plus running hooks. When that limit is
reached, the sync waits for a hook slot before submitting another one. The sync
command still waits for all submitted hooks before exiting. Hook failures are
logged and do not abort the sync; failed hooks are not automatically retried.

## Immich integration

The recommended way to get captures into Immich is to point **immich-go** at the
output folder. Because the game name lives only in the folder path, the recipe
invokes immich-go once per game folder and sets an explicit `--tag` to the folder
name (the built-in `--folder-as-tags` tags with a root-prefixed path such as
`psn-captures/Example Game`, not the bare game name).

See [docs/immich.md](docs/immich.md) for the full recipe: install/pin, config
file (API key off the command line), uploading the existing backlog, the
systemd timer that keeps it in sync automatically, and the album-per-game
alternative.

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
4. **NPSSO expiry (~60 days).** The NPSSO cookie is short-lived. You must
   periodically paste a fresh one; the tool logs a clear, actionable error when
   auth fails. This is unavoidable without a full 2FA login flow, which is out of
   scope.
5. **Breakage after PS App updates.** The client credentials and endpoint
   shapes could change at any PlayStation App release, which may break listing or
   downloads until this project is updated.
