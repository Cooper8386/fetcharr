# Changelog

## v0.2.0 — 2026-06-29

### Fixed

- **"Now Pulling" stayed at 0% / 0 B/s for the entire transfer.** rclone
  was emitting stats lines in the `GBytes`/`Bytes` long form (and decorated
  with `NOTICE:` timestamps) that the regex didn't accept. New regex
  handles every documented rclone unit form, the log-decorated variant,
  and the `ETA -` startup edge case.
- Lowered `--stats` interval from 2s to 1s so the UI updates faster.

### Added

- **Bytes-on-disk fallback for the progress bar.** While a pull is
  running, a separate thread `du`'s the destination directory every 2s.
  The UI now picks `max(rclone_bytes, disk_bytes)` so the progress bar
  moves even if rclone's stats line is malformed or silent. Bar uses a
  diagonal-stripe pattern when the disk-scan number is being used.
- **60-second speed sparkline** under the Now Pulling card with peak +
  current readouts.
- **Config page at /config** for live-editing the common tunables:
  `poll_interval`, `stability_seconds`, `min_free_gb`, `rclone_transfers`,
  `rclone_checkers`, `rclone_bwlimit`. Changes apply on the next poll
  cycle without a container restart AND are persisted back to
  `/config/config.yml` so they survive restarts.
- **Speed unit toggle** (MB/s ↔ Mbps) in the UI header, persisted per
  browser via localStorage. Doesn't affect `/metrics`.
- **`/api/config`** GET/POST endpoints for the same fields, with input
  validation (min/max bounds, type coercion).
- **runtime overrides** in AppState so poller and worker re-read tunables
  on each cycle/job-start instead of capturing them at process boot.

### Internal

- `parse_stats_line` now handles short-form, long-form, and NOTICE-decorated
  rclone output. Existing samples + new edge cases all unit-tested in CI.
- Worker reads tunables via `app.get_runtime()` with fallback to Config.
- Poller reads tunables on every cycle.

## v0.1.0 — 2026-06-29

Initial public release.

### seedbox-fetcher

- Pull-based seedbox → NAS delivery via rclone SFTP (no Syncthing dependency).
- Bootstrap snapshot on first run so pre-existing releases are not re-pulled.
- Cutoff mtime filter for additional safety against state loss.
- Optional Sonarr/Radarr `/api/v3/history` filter to skip releases *arr
  already imported.
- Stability gate: only pulls a release whose size + file count have been
  steady for `stability_seconds` (default 90s).
- Single-page web UI on port 8765 with live transfer progress, release
  table, action buttons (re-pull, mark-pulled, forget, pause, cancel),
  and a recent log panel.
- Prometheus `/metrics` endpoint and `/healthz`.
- Server-Sent Events for live UI updates.
- Apprise notifications and optional Healthchecks.io pings.
- CSRF token on action endpoints.

### sab-watchdog

- Polls SABnzbd's queue, detects jobs stuck in post-processing states.
- Three-stage escalation: Apprise alert → `pause_pp` + `resume_pp` →
  `restart_repair`.
- Never deletes a job; relies on *arr Failed Download Handling for cleanup.

### scripts/cleanUpDownloadStaging.sh

- 24h retention (configurable) cleanup of NAS torrent staging folders.
- Fail-closed on Sonarr/Radarr API errors.
- API keys read from `.cleanup.env` instead of being hard-coded.
- Single-instance lock via `flock`.
- Protects both queue items AND recent `eventType=grabbed` history.
- `DRY_RUN=1` for safe previews.
- Optional Healthchecks.io heartbeat.
