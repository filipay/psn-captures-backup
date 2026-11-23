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

  The release, flag, and checksum details below were checked against v0.32.0 on
  2026-11-23.

- An Immich **API key** (Immich → Account Settings → API Keys) with at least:
  `asset.read`, `asset.statistics`, `asset.update`, `asset.upload`,
  `album.create`, `album.read`, `albumAsset.create`, `server.about`,
  `stack.create`, `tag.asset`, `tag.create`, `user.read`. Add `job.create` +
  `job.read` only if you pause Immich jobs (see the `--pause-immich-jobs` note
  below).

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
- `--recursive` is the default (`true`) and need not be passed.
- `--concurrent-tasks=4` enables concurrent processing; it is a root-level flag and must appear before `upload`.

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
> `--into-album "PlayStation Captures"` against the output root instead — but
> first move the state database and token file out of that root with
> `PSN_STATE_FILE` and `PSN_TOKEN_FILE`, or confirm the consumer ignores
> dotfiles. The default `.psn-token.json` is a secret and must not be uploaded
> or indexed by immich-go, rclone, Jellyfin, or a NAS scanner.

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

## Automate it with the post-download hook

The backup already knows exactly when a capture has finished downloading, so a
filesystem watcher or polling timer is unnecessary. Use the built-in
`PSN_POST_DOWNLOAD_SCRIPT` hook and enable its debounce mode:

```env
PSN_POST_DOWNLOAD_SCRIPT=/usr/local/bin/psn-captures-to-immich
PSN_POST_DOWNLOAD_DEBOUNCE_SECONDS=10
```

`PSN_POST_DOWNLOAD_DEBOUNCE_SECONDS` is a quiet period: every successful
download resets the timer. Once no new capture has finished for 10 seconds, the
hook is invoked once with all captures from that burst. Set it to `0` to retain
the original one-hook-per-download behavior.

### Immich hook script

Because the hook can receive several files, group them by game folder and scan
each affected game once. Save this as
`/usr/local/bin/psn-captures-to-immich` and `chmod +x` it:

```bash
#!/bin/sh
set -eu

CONFIG=/host-immich/config/immich-go.toml
IMMICH_GO=/host-immich/immich-go
QUEUE=$(mktemp)
trap 'rm -f "$QUEUE"' EXIT

for file in "$@"; do
  dirname "$file"
done | sort -u > "$QUEUE"

while IFS= read -r game_dir; do
  [ -d "$game_dir" ] || continue
  "$IMMICH_GO" --config "$CONFIG" --concurrent-tasks=4 \
    upload from-folder --no-ui --on-errors continue --pause-immich-jobs=false \
    --into-album "PlayStation Captures" \
    --tag "$(basename "$game_dir")" \
    "$game_dir"
done < "$QUEUE"
```

The hook invokes immich-go once for each affected game folder, so ten captures
that finish close together do not cause ten full folder scans.

### Removing local files after upload

There is **no built-in removal**. `processed` is only the hook's exit signal
(`0` = the script succeeded); it does not mean "safe to delete" and the backup
never deletes anything itself. Removal is entirely up to your hook, and is only
safe after the upload for those specific files succeeded.

Use this variant of the hook script when you want the local copies removed. It
drops `--on-errors continue`, so a failed upload stops the script before that
folder's files are deleted, and it only removes the files the hook was called
with (not the rest of the game folder):

```bash
#!/bin/sh
set -eu

CONFIG=/host-immich/config/immich-go.toml
IMMICH_GO=/host-immich/immich-go
QUEUE=$(mktemp)
trap 'rm -f "$QUEUE"' EXIT

for file in "$@"; do
  dirname "$file"
done | sort -u > "$QUEUE"

while IFS= read -r game_dir; do
  [ -d "$game_dir" ] || continue
  "$IMMICH_GO" --config "$CONFIG" --concurrent-tasks=4 \
    upload from-folder --no-ui --pause-immich-jobs=false \
    --into-album "PlayStation Captures" \
    --tag "$(basename "$game_dir")" \
    "$game_dir"

  for file in "$@"; do
    case "$file" in
      "$game_dir"/*) rm -f -- "$file" ;;
    esac
  done
done < "$QUEUE"
```

Because `set -e` aborts on a nonzero immich-go exit, no files are removed after
a failed folder upload. Files from folders that already uploaded successfully
may have been removed before a later folder fails; that is correct, since those
were uploaded. Note that without `--on-errors continue` one bad asset fails the
whole folder and keeps its files locally.

When the script exits `0`, the captures are recorded as `processed`; a nonzero
exit or timeout leaves them as `downloaded`. Both are skipped on later syncs
because the state DB is the source of truth, so deleting files never causes a
re-download. If you need them back, `psn-captures-backup sync --redownload`
re-fetches recorded captures within PSN's retention window (around 14 days).

Each hook invocation has a **300-second limit**. A long backlog upload can hit
it, leaving the files in place with status `downloaded` and no automatic retry.
For large backlogs, run the upload out-of-band and keep the hook short.

Hooks only run for fresh downloads, so neither an upload that was never
attempted nor one whose hook failed/timed out is retried by the backup. And once
the files are deleted, Immich holds the only copy: verify a small batch before
enabling removal, and treat upload failures as keep-the-files failures.

### Docker

The post-download hook is **a host script**, not part of the backup image. The
container only needs to execute the script with the completed capture paths.
Keep `immich-go`, its config, and the script on the host and bind-mount them
read-only:

```yaml
services:
  psn-captures-backup:
    # ...existing settings...
    env_file: .env
    environment:
      PSN_POST_DOWNLOAD_SCRIPT: /host-immich/psn-captures-to-immich
      PSN_POST_DOWNLOAD_DEBOUNCE_SECONDS: 10
    volumes:
      - /path/to/psn-captures:/captures
      - /usr/local/bin/immich-go:/host-immich/immich-go:ro
      - /etc/immich-go:/host-immich/config:ro
      - /usr/local/bin/psn-captures-to-immich:/host-immich/psn-captures-to-immich:ro
```

The host script can then invoke the mounted host binary and config using the
paths visible inside the container. No `immich-go` files need to be added to
the backup image.

The container user must be able to execute the mounted script and binary, and
read the config and capture files. Keep the Immich API key in the config file
rather than putting it on the command line.


### Alternative: filesystem watcher

A periodic or filesystem-driven host-side process is still a valid alternative
when you prefer the backup container to remain completely unaware of Immich.
For example, `inotify-tools` can trigger the same host upload script whenever
a completed capture appears.

This is optional and is not needed when the post-download hook is enabled. It
also introduces an extra host package and another long-running service, so the
hook-based approach is the simpler default.

### One-time backlog upload

Debouncing only affects newly downloaded captures. Upload an existing backlog
once with the normal per-game loop:

```bash
CAPTURES=/path/to/psn-captures

for d in "$CAPTURES"/*/; do
  [ -d "$d" ] || continue
  immich-go --config /etc/immich-go/immich-go.toml --concurrent-tasks=4 \
    upload from-folder --no-ui --on-errors continue --pause-immich-jobs=false \
    --into-album "PlayStation Captures" \
    --tag "$(basename "$d")" \
    "$d"
done
```

Because dedupe is checksum-based, repeating this command is safe.

## Idempotency, exit codes, and safety

- Duplicate detection is **checksum-based (SHA-1 of file content)** plus
  filename/size; existing assets are skipped by default. Interrupted uploads
  resume safely, so re-running after each sync is a no-op for unchanged files.
- There is **no persistent local state** for `from-folder`: each run re-walks
  the tree and rebuilds an in-memory index of server assets. The backup's hook
  batching avoids launching a second scan for every capture in a burst.
- Exit codes: `0` success, `1` error. Use `--no-ui` for non-interactive runs;
  add `--log-file /var/log/immich-go/upload.log` if you want a persistent log.
- `--overwrite` (default `false`) forces replacement of server assets with the
  local version. Leave it off for normal syncing.

## Notes

- Pin `v0.32.0`. Recent breaking changes worth knowing: the server-error flag
  was renamed `--server-errors` → `--on-errors` (v0.30.0); concurrency moved to
  the root command and was renamed `--concurrent-uploads` → `--concurrent-tasks`;
  config-file support landed in v0.29.0.
- The backup's filename layout (including the timestamp fallback) is documented
  once in the README under
  [Output layout](../README.md#output-layout). immich-go uses the filename date
  only when the media has no creation metadata, so assets still land
  chronologically.
- Immich removed `deviceAssetId`/`deviceId` in v3; dedupe is checksum-based, so
  repeated uploads of the same files are no-ops.
- The recommended recipe has been exercised end-to-end against a live Immich
  server (upload of an existing capture library succeeded).
