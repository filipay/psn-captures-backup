# Immich integration (immich-go)

This tool's job is to download captures into a predictable folder. Getting them
into [Immich](https://immich.app/) is done with
[**immich-go**](https://github.com/simulot/immich-go), which consumes the output
folder directly. Immich is intentionally not a dependency of the core tool.

Throughout this guide, `<captures>` is the host folder the container writes to
(for example `/path/to/psn-captures`); the container sees it as `/captures`.

Because the **game name only exists in the folder path**
(`<captures>/<game>/<date>_<id>.<ext>`), the recipe below sets an explicit
per-game tag so the game is preserved inside Immich. Uploading the folder with
the album alone would lose that grouping.

## Prerequisites

- The backup container writes to a host folder, e.g. `<captures>`.
- `immich-go` **v0.32.0** (or newer) installed on the host. There is **no
  official Docker image**, so install the static binary from the release:

  ```bash
  curl -L -o /tmp/immich-go.tar.gz \
    https://github.com/simulot/immich-go/releases/download/v0.32.0/immich-go_Linux_x86_64.tar.gz
  # verify the SHA against checksums.txt on the release page
  tar -xzf /tmp/immich-go.tar.gz -C /tmp immich-go
  sudo install -m 0755 /tmp/immich-go /usr/local/bin/immich-go
  immich-go --version
  ```

- An Immich **API key** (Immich → Account Settings → API Keys) with at least:
  `asset.read`, `asset.statistics`, `asset.update`, `asset.upload`,
  `asset.copy`, `asset.delete`, `asset.download`, `album.create`, `album.read`,
  `albumAsset.create`, `server.about`, `stack.create`, `tag.asset`,
  `tag.create`, `user.read`. Add `job.create` + `job.read` only if you pause
  Immich jobs (see the `--pause-immich-jobs` note below).

## Configuration (keep the API key off the command line)

immich-go reads a config file (TOML/YAML/JSON). It only searches the **current
working directory** by default, so pass an explicit absolute path with
`--config`. Store the key in a root-owned file:

`/etc/immich-go/immich-go.toml` (`chmod 600`):

```toml
[upload]
server = "https://immich.example.com"
api-key = "YOUR_API_KEY"
no-ui = true
pause-immich-jobs = false
```

Environment variables work too, but the prefix is **`IMMICH_GO_`**, not
`IMMICHGO_`, and they are command-scoped:

- `IMMICH_GO_UPLOAD_SERVER`
- `IMMICH_GO_UPLOAD_API_KEY`

Precedence is **CLI flag > environment > config file > default**.

## Recommended recipe: one album + per-game tags

Uploads use `--concurrent-tasks=4` (a global flag, placed before `upload`) to process up to four tasks concurrently. Adjust the value based on host and server capacity.

immich-go is invoked **once per game folder** so the tag can be set explicitly
to the folder name (deterministic — it does not depend on how immich-go
interprets nested paths):

```bash
CAPTURES=/path/to/psn-captures
for d in "$CAPTURES"/*/; do
  immich-go --config /etc/immich-go/immich-go.toml --concurrent-tasks=4 \
    upload from-folder --no-ui --on-errors continue --pause-immich-jobs=false \
    --into-album "PlayStation Captures" \
    --tag "$(basename "$d")" \
    "$d"
done
```

- `--into-album "PlayStation Captures"` puts everything into one album (created
  if absent).
- `--tag "$(basename "$d")"` tags each asset with its game folder name.
- `--on-errors continue` lets the batch finish even if one asset fails.
- `--pause-immich-jobs=false` avoids pausing/resuming server jobs on every
  invocation (relevant for frequent runs; with this flag the API key does not
  need `job.*` permissions).
- `--recursive` is the default (`true`) and need not be passed.\n- `--concurrent-tasks=4` enables concurrent processing; it is a root-level flag and must appear before `upload`.

### Uploading the existing backlog

Preview a single game folder, then upload everything already downloaded by
running the same loop above once without `--dry-run`:

```bash
CAPTURES=/path/to/psn-captures

# --dry-run is a GLOBAL flag, so it goes before the subcommand
immich-go --config /etc/immich-go/immich-go.toml --dry-run --concurrent-tasks=4 \
  upload from-folder --no-ui --pause-immich-jobs=false \
  --into-album "PlayStation Captures" \
  --tag "Example Game" \
  "$CAPTURES/Example Game"
```

Because dedupe is checksum-based, re-running the whole thing is safe: assets
already in Immich are skipped.

> `--flat` caveat: with `--flat` there are no per-game folders, so the loop has
> nothing to iterate and there are no per-game tags to derive. Use
> `--into-album "PlayStation Captures"` against the output root instead.

## Tag semantics: why the explicit `--tag`

The built-in `--folder-as-tags` flag does **not** produce the bare game name. It
tags each asset with `<basename of the root you passed>/<relative path>`. For
example, running it against `<captures>` yields `psn-captures/Example Game` (and
`psn-captures` for root-level files), not `Example Game`. This was verified
against immich-go v0.32.0 source and its own unit tests.

That is why the recommended recipe sets `--tag` explicitly per game folder
instead of relying on `--folder-as-tags`. If you prefer one album **per game**
rather than tags, use `--folder-as-album=FOLDER` instead (album title = folder
name); do not combine it with `--into-album`.

## Automate it (systemd + inotify watcher)

immich-go does not provide a built-in folder-watch mode, so the simplest
event-driven setup is to use Linux's `inotify` facility to wake a long-running
systemd service when a new capture appears. This avoids a fixed polling timer
and typically starts the Immich upload within a few seconds.

Install `inotify-tools` on the host:

Arch Linux:
```bash
sudo pacman -S inotify-tools
```

Debian/Ubuntu:
```bash
sudo apt install inotify-tools
```

Save the watcher as `/usr/local/bin/psn-captures-to-immich-watch` and `chmod +x`:

```bash
#!/usr/bin/env bash
set -euo pipefail

CONFIG=/etc/immich-go/immich-go.toml
CAPTURES=/path/to/psn-captures
DEBOUNCE_SECONDS=3

declare -A pending=()

queue_path() {
  local path=$1

  # Only react to completed media files. PSN writes to .part and then
  # atomically renames the completed file into place.
  [ -f "$path" ] || return
  case "$path" in
    *.part|*/.*) return
  esac

  local rel game_dir
  rel="${path#"$CAPTURES"/}"

  # The first path component is the game folder. Ignore root-level files.
  [[ "$rel" == */* ]] || return

  game_dir="$CAPTURES/${rel%%/*}"
  pending["$game_dir"]=1
}

upload_pending() {
  local game_dir

  for game_dir in "${!pending[@]}"; do
    [ -d "$game_dir" ] || continue

    # Serialize this with any manual backlog run. Concurrency is handled
    # inside immich-go rather than by starting competing scans.
    flock /run/immich-go-psn.lock \
      immich-go --config "$CONFIG" --concurrent-tasks=4 \
      upload from-folder --no-ui --on-errors continue --pause-immich-jobs=false \
      --into-album "PlayStation Captures" \
      --tag "$(basename "$game_dir")" \
      "$game_dir"
  done

  pending=()
}

inotifywait -m -r \
  -e moved_to,close_write \
  --format '%w%f\0' \
  "$CAPTURES" |
while IFS= read -r -d '' path; do
  queue_path "$path"

  # Debounce bursts of captures. New events reset the quiet period.
  while IFS= read -r -d '' -t "$DEBOUNCE_SECONDS" path; do
    queue_path "$path"
  done

  upload_pending
done
```

Create `/etc/systemd/system/immich-go-psn-watch.service`:

```ini
[Unit]
Description=Watch PSN captures and upload them to Immich
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/psn-captures-to-immich-watch
Restart=on-failure
RestartSec=5

# Run as the user that can read the capture tree and immich-go config.
# Omit these lines to run as root:
# User=youruser
# Group=yourgroup

[Install]
WantedBy=multi-user.target
```

Enable the watcher:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now immich-go-psn-watch.service
systemctl status immich-go-psn-watch.service
journalctl -u immich-go-psn-watch.service -f
```

If you previously used the 10-minute timer, disable it so the two mechanisms
do not both scan the library:

```bash
sudo systemctl disable --now immich-go-psn.timer
```

### Why this is preferable to a per-file trigger

The PSN backup writes each capture atomically (`.part` -> final filename), so
the watcher reacts to the completed file rather than trying to upload a partial
download.

The watcher deliberately **does not start immich-go for every event**. It waits
for a short quiet period, groups changed files by game folder, then runs one
`from-folder` scan for each affected game. immich-go's checksum-based dedupe
makes re-scanning the folder safe.

This preserves the explicit per-game `--tag` behavior from the manual recipe.
`--concurrent-tasks=4` controls concurrency inside each immich-go run; the
watcher processes affected game folders serially, avoiding competing scans.

For a large capture library, `inotify` can hit the kernel's per-user watch
limit. If the service reports that it cannot add watches, check:

```bash
sysctl fs.inotify.max_user_watches
```

and increase it with a persistent sysctl setting if necessary.

### One-time backlog upload

The watcher only reacts to new filesystem events, so upload the existing backlog
once with the normal per-game loop:

```bash
CAPTURES=/path/to/psn-captures

for d in "$CAPTURES"/*/; do
  [ -d "$d" ] || continue
  flock /run/immich-go-psn.lock \
    immich-go --config /etc/immich-go/immich-go.toml --concurrent-tasks=4 \
    upload from-folder --no-ui --on-errors continue --pause-immich-jobs=false \
    --into-album "PlayStation Captures" \
    --tag "$(basename "$d")" \
    "$d"
done
```

The `flock` keeps a manual backlog upload and the watcher from running
simultaneously.

## Idempotency, exit codes, and safety

- Duplicate detection is **checksum-based (SHA-1 of file content)** plus
  filename/size; existing assets are skipped by default. Interrupted uploads
  resume safely, so re-running after each sync is a no-op for unchanged files.
- There is **no persistent local state** for `from-folder`: each run re-walks
  the tree and rebuilds an in-memory index of server assets. Occasional overlap
  is handled by the `flock` in the service above.
- Exit codes: `0` success, `1` error. Use `--no-ui` for non-interactive runs;
  add `--log-file /var/log/immich-go/upload.log` if you want a persistent log.
- `--overwrite` (default `false`) forces replacement of server assets with the
  local version. Leave it off for normal syncing.

## Notes

- Pin `v0.32.0`. Recent breaking changes worth knowing: the server-error flag
  was renamed `--server-errors` → `--on-errors` (v0.30.0); concurrency moved to
  the root command and was renamed `--concurrent-uploads` → `--concurrent-tasks`;
  config-file support landed in v0.29.0.
- Filenames are `<YYYY-MM-DD>_<capture-id>.<ext>`. immich-go normally takes the
  capture date from embedded EXIF/video metadata first; the filename is only a
  fallback, and its documented `--date-from-name` pattern expects a time
  component, so it may not parse. Check with `--dry-run` if dates look wrong.
- Immich removed `deviceAssetId`/`deviceId` in v3; dedupe is checksum-based, so
  repeated uploads of the same files are no-ops.
- The recommended recipe has been exercised end-to-end against a live Immich
  server (upload of an existing capture library succeeded).
