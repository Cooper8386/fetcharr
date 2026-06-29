# fetcharr

A robust seedbox → NAS media pipeline for Sonarr/Radarr setups that use a
remote seedbox for torrents and a local Usenet downloader.

[![Build seedbox-fetcher](https://github.com/Cooper8386/fetcharr/actions/workflows/build-fetcher.yml/badge.svg)](https://github.com/Cooper8386/fetcharr/actions/workflows/build-fetcher.yml)
[![Build sab-watchdog](https://github.com/Cooper8386/fetcharr/actions/workflows/build-watchdog.yml/badge.svg)](https://github.com/Cooper8386/fetcharr/actions/workflows/build-watchdog.yml)
[![Lint](https://github.com/Cooper8386/fetcharr/actions/workflows/lint.yml/badge.svg)](https://github.com/Cooper8386/fetcharr/actions/workflows/lint.yml)

## What it does

If your media stack looks like this:

- Torrents downloaded on a remote seedbox (qBittorrent)
- Usenet downloaded locally on the NAS (SABnzbd)
- Sonarr/Radarr/Plex/Overseerr on the NAS
- Files need to make it from seedbox → NAS so *arr can import them

…then the seedbox → NAS step is usually the fragile part. fetcharr replaces
the common `Syncthing + post-download hook script + cleanup cron` chain with
two small containers that handle delivery and self-healing.

### Three pieces

**seedbox-fetcher** — a small Python daemon that watches your seedbox via
`rclone` over SFTP, pulls newly completed releases into NAS staging folders,
and exposes a web UI with live transfer progress, Prometheus `/metrics`,
and action buttons (re-pull, cancel, pause, etc.).

**sab-watchdog** — polls SABnzbd's queue. If a job is stuck in
`Trying RAR renamer` (or any other PP state) it alerts you, then bounces
post-processing, then calls `restart_repair`. Never deletes anything.

**cleanUpDownloadStaging.sh** — a User-Script-style bash script that
deletes leftover release folders from `/data/torrents/...` after 24h,
fail-closed on Sonarr/Radarr queue API failures, with `DRY_RUN=1` for safe
previews.

## Why not Syncthing + a category-driven hook script?

That setup has two well-known fragility modes:

1. SABnzbd occasionally hangs on `Trying RAR renamer`. Manual intervention.
2. If qBittorrent's `%L` (category) is empty at completion time, the post-hook
   script can't route the file, and Sonarr never sees the import.

fetcharr's pull model removes both:
- The NAS pulls from the seedbox, so the seedbox never has to push.
- A wrong qBit category only affects where qBit saves the torrent, not whether
  the NAS sees it — anything under `downloads/qbittorrent/remote-{tv,movies}`
  gets fetched.

## Quick start

```bash
# On your NAS (Unraid example paths)
mkdir -p /mnt/user/appdata/fetcharr/{seedbox-fetcher,sab-watchdog}/state

# Drop your rclone config + SSH key into seedbox-fetcher/
# (Generate rclone.conf once on the host with `rclone config` first.)
cp ~/.config/rclone/rclone.conf /mnt/user/appdata/fetcharr/seedbox-fetcher/rclone.conf
cp ~/.ssh/id_seedbox            /mnt/user/appdata/fetcharr/seedbox-fetcher/id_seedbox
chown -R 99:100                 /mnt/user/appdata/fetcharr/seedbox-fetcher/
chmod 600                       /mnt/user/appdata/fetcharr/seedbox-fetcher/id_seedbox

# Get the compose file and start it
cd /mnt/user/appdata/fetcharr
curl -fsSL https://raw.githubusercontent.com/Cooper8386/fetcharr/main/docker-compose.yml -o docker-compose.yml
docker compose up -d

# First run writes a default config.yml. Edit it and restart.
nano /mnt/user/appdata/fetcharr/seedbox-fetcher/config.yml
docker compose restart seedbox-fetcher
```

Then point your browser at `http://nas-ip:8765`.

Full step-by-step setup is in [docs/deployment.md](docs/deployment.md).

## Web UI

The fetcher serves a single-page UI at port 8765:

- Live transfer progress (percent, transferred/total, speed, ETA)
- Releases table with per-release status badges
- Action buttons: re-pull, mark-as-pulled, forget, pause/resume, cancel
- Recent log
- Prometheus `/metrics` for Grafana

LAN-only by default. There is no authentication — put it behind your existing
reverse proxy / Authelia / Tailscale if you want it on the internet.

## How it decides what to pull

Layered filters, fast first:

1. **State** — already pulled? Skip.
2. **Bootstrap** — first run snapshots everything currently on the seedbox as
   "already-pulled" so the fetcher only ever pulls *new* arrivals.
3. **Cutoff mtime** — anything older than `cutoff_mtime` is treated as
   already-handled. Belt-and-suspenders with bootstrap; protects you if
   state.yml is ever wiped.
4. **Stability gate** — release must report the same size+file count for
   `stability_seconds` (default 90s) before being pulled. Prevents pulling
   half-finished torrents.
5. **Local-exists check** — if the destination already has a folder with the
   release name, mark pulled and skip.
6. **Sonarr/Radarr history check (optional, off by default)** — if *arr
   already imported the release more than `grace_minutes` ago, skip.

## Repo layout

```
fetcharr/
├── docker-compose.yml             # uses published GHCR images
├── docker-compose.build.yml       # alternate: build from local source
├── seedbox-fetcher/               # the fetcher container source
├── sab-watchdog/                  # the watchdog container source
├── scripts/                       # cleanUpDownloadStaging.sh + rclone mount
├── config-examples/               # reference configs
└── docs/
    ├── architecture.md
    ├── deployment.md
    └── troubleshooting.md
```

## Hardware notes

Designed for an Unraid box. Tested against a Whatbox/SBI-style seedbox over
SFTP. Should work on any Linux + Docker host. The fetcher uses ~80 MB RAM
idle, ~150 MB during a pull. CPU is dominated by rclone's SFTP cipher, not
Python.

## Status

This is one person's personal homelab tool, made public in case others find
it useful. It works for me. Issues and PRs welcome but no support promises.

## License

MIT — see [LICENSE](LICENSE).
