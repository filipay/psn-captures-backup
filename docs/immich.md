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

Uploads use `--concurrent-tasks=4` (a global flag, placed before `upload`) to process up to four tasks concurrently. Adjust the value based on host and server capacity.\n\nimmich-go is invoked **once per game folder** so the tag can be set explicitly
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

## Automate it (systemd timer)

Save the wrapper as `/usr/local/bin/psn-captures-to-immich` and `chmod +x`:

```bash
#!/usr/bin/env bash
set -euo pipefail

CONFIG=/etc/immich-go/immich-go.toml
CAPTURES=/path/to/psn-captures

for d in "$CAPTURES"/*/; do
  [ -d "$d" ] || continue
  immich-go --config "$CONFIG" --concurrent-tasks=4 \
    upload from-folder --no-ui --on-errors continue --pause-immich-jobs=false \
    --into-album "PlayStation Captures" \
    --tag "$(basename "$d")" \
    "$d"
done
```

`/etc/systemd/system/immich-go-psn.service` — `flock` prevents overlapping runs:

```ini
[Unit]
Description=Upload PSN captures to Immich
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
# Run as the user that owns the capture files (needed when <captures> is a bind
# mount). Uncomment and set:
# User=youruser
# Group=yourgroup
ExecStart=/usr/bin/flock -n /run/immich-go-psn.lock /usr/local/bin/psn-captures-to-immich
```

`/etc/systemd/system/immich-go-psn.timer`:

```ini
[Unit]
Description=Periodic PSN captures upload to Immich

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now immich-go-psn.timer
systemctl list-timers immich-go-psn.timer
```

Versus running it by hand, the timer keeps new captures flowing without any
manual step; see the README for why the container itself must also run as a uid
able to write the mounted folder.

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
