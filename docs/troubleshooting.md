# Troubleshooting

## seedbox-fetcher

### `groupadd: GID 'NNN' already exists` then container crash-loops

The base image already has a group at your `PGID`. Fixed in the
entrypoint by reusing existing UID/GID — make sure you're on the latest
image:

```bash
docker compose pull
docker compose up -d
```

### `failed to read private key file: open /config/id_seedbox: permission denied`

The container runs as UID `${PUID:-99}` and can't read your key. Run on the host:

```bash
chown 99:100 /mnt/user/appdata/fetcharr/seedbox-fetcher/id_seedbox
chmod 600    /mnt/user/appdata/fetcharr/seedbox-fetcher/id_seedbox
docker restart seedbox-fetcher
```

### `rclone probe failed at startup`

`docker exec seedbox-fetcher rclone --config /config/rclone.conf lsd seedbox:`
will print the actual rclone error. Common causes:

- `rclone.conf` references a host-side path for `key_file` instead of the
  container-side `/config/id_seedbox`.
- SSH key is the wrong identity / not authorized on the seedbox.
- Host firewall blocks outbound port 22.
- Seedbox provider's SSH host fingerprint changed; clear `known_hosts` or
  set `host_key_check = false` in rclone.conf (less safe).

### `source for remote-tv not present: /mnt/seedbox/...`

You're running an OLD config that still uses `source: /mnt/seedbox/...`
(the FUSE-mount style). Switch to the rclone-direct style:

```yaml
routes:
  - name: remote-tv
    remote_path: downloads/qbittorrent/remote-tv
    dest: /data/torrents/remote-tv
```

### Fetcher pulled something I didn't want

Open the UI, find the release, click "forget" to remove it from state
entirely. Then delete the local copy:

```bash
rm -rf /mnt/user/data/torrents/remote-tv/<release-name>
```

If it was pulled because state was wiped and `cutoff_mtime` wasn't set,
add a `cutoff_mtime` to `config.yml` and restart.

### Fetcher won't pull a release that's clearly done

Check the UI's release row → status:

| Status | What it means | Fix |
|---|---|---|
| `pulled` | already handled | click "re-pull" if you want it again |
| `bootstrap` | predates this tool | click "re-pull" if you want it again |
| `cutoff_skipped` | older than `cutoff_mtime` | edit cutoff in config, then "forget" + restart |
| `arr_skipped` | *arr already imported >grace ago | click "re-pull" if you want it again |
| `pending` | stable gate hasn't elapsed | wait `stability_seconds` (default 90s) |

### Pull cancelled and partial files left in /data/

The worker is supposed to `rmtree` the partial dest on cancel. If a partial
remains anyway (rare race), remove it manually:

```bash
rm -rf /mnt/user/data/torrents/remote-tv/<partial-release>
```

Then click "forget" on it in the UI so the fetcher will re-evaluate it.

### Web UI: 502 / connection refused

- Container is up but uvicorn died: `docker logs seedbox-fetcher | grep -i uvicorn`
- Port 8765 is taken by something else: change `web.port` in config.yml AND
  the `ports:` entry in docker-compose.yml.
- Behind a reverse proxy: the SSE endpoint needs `proxy_buffering off;` and
  a long read timeout (e.g. `proxy_read_timeout 3600;` on nginx).

## sab-watchdog

### Bouncing post-processing but jobs still stuck

The watchdog only knows three escalations: alert, bounce, restart_repair. If
restart_repair didn't help either, the job is genuinely broken (encrypted
RAR with bad password, corrupt par2, etc.). Manually delete from SAB and
re-grab from Sonarr/Radarr.

### Wants my API key but Apprise URL didn't take effect

`SAB_API_KEY` must be set either as an env var on the container or in
`config.yml`. Apprise URLs are in `config.yml` (`apprise_urls:` list) or
the `APPRISE_URLS` env var (comma-separated).

## cleanUpDownloadStaging.sh

### Refuses to delete anything: `ABORT: Sonarr unreachable; refusing to delete`

By design. The script fails closed if either *arr's API is down. Check:

```bash
curl -sS -H "X-Api-Key: $SONARR_API_KEY" "$SONARR_URL/api/v3/queue?pageSize=1"
```

### Deleted something it shouldn't have

Three things have to be true for the script to delete a folder:

1. Folder older than `RETENTION_MINUTES` (default 1440 = 24h).
2. Folder basename does NOT appear in Sonarr's queue (queue + recent history
   eventType=grabbed).
3. Folder basename does NOT appear in Radarr's queue + recent history.

If it deleted something that's still a valid release on *arr's side, you
probably hit:

- The release was renamed by *arr before being added to the queue (rare).
- The release was added to *arr but the queue API was empty at the moment
  the script ran (race).

The safe knob is to bump `RETENTION_MINUTES` higher.

## General

### "Files are owned by root inside /data/torrents/..."

Container PUID/PGID don't match Unraid's `nobody:users` (99:100). Set:

```yaml
environment:
  - PUID=99
  - PGID=100
```

in your docker-compose.yml service entry, then `docker compose up -d`.

### Updating to a new fetcharr version

```bash
cd /mnt/user/appdata/fetcharr
docker compose pull
docker compose up -d
```

To pin a specific version, change `:latest` in docker-compose.yml to
`:v0.2.0` (or whatever tag) and `docker compose up -d`.

### How do I see what tags exist?

[GHCR fetcharr-fetcher tags](https://github.com/Cooper8386/fetcharr/pkgs/container/fetcharr-fetcher)
and [GHCR fetcharr-watchdog tags](https://github.com/Cooper8386/fetcharr/pkgs/container/fetcharr-watchdog).
