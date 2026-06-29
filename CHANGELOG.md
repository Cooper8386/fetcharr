# Changelog

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
