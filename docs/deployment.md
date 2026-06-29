# Deployment guide

Tested on Unraid. The conventions in this guide are Unraid-flavored
(`/mnt/user/...`, User Scripts plugin, Compose Manager plugin) but the
same files work on any Linux + Docker host with minor path tweaks.

## What you'll set up

- rclone SFTP mount of your seedbox at `/mnt/seedbox` (optional — only for
  manual browsing; the containers don't depend on it)
- `seedbox-fetcher` and `sab-watchdog` containers
- `cleanUpDownloadStaging.sh` as a User Script on a 30-minute schedule
- SAB tuning + Sonarr/Radarr settings

## Prereqs

- Docker on Unraid (you already have this).
- Compose Manager plugin OR comfort with `docker compose` over SSH.
- An SSH keypair authorized on your seedbox for passwordless login.
- A shared Docker mount root that Sonarr/Radarr/SAB also see. In this guide
  that's `/mnt/user/data`, with subfolders `torrents/{remote-tv,remote-movies}`
  and `media/{tv,movies}`.

## Step 1: SSH key + rclone remote

On the NAS, as root:

```bash
ssh-keygen -t ed25519 -f /root/.ssh/id_seedbox -N ''
ssh-copy-id -i /root/.ssh/id_seedbox.pub onion8386@your.seedbox.example.com
```

Configure an rclone remote:

```bash
rclone config
# n) new remote
#   name: seedbox
#   storage: sftp
#   host: your.seedbox.example.com
#   user: onion8386
#   port: 22
#   key_file: /config/id_seedbox      <- container-side path
#   shell_type: unix
#   md5sum_command: md5sum
#   sha1sum_command: sha1sum
# Save.
```

> **Important:** the `key_file` in `rclone.conf` must be the path **inside
> the container** (`/config/id_seedbox`), not the host path. We'll mount your
> key into `/config/` in step 5.

Test the remote works at all:

```bash
rclone lsd seedbox:downloads/qbittorrent
# Should list remote-tv, remote-movies, etc.
```

## Step 2: optional — host-level rclone mount

Only if you want to be able to `ls /mnt/seedbox/...` from the Unraid CLI to
manually browse seedbox contents. Not required by the fetcher.

User Scripts plugin → add a new script called `rclone-seedbox-mount`. Paste
the contents of [`scripts/rclone-seedbox-mount.unraid.sh`](../scripts/rclone-seedbox-mount.unraid.sh).
Schedule: **At Startup of Array**. Also schedule the same script `*/5 * * * *`
as a remount-if-needed safety net.

## Step 3: SAB tuning (GUI)

Open SAB, change these settings — these match a "copy + delete, no NAS
seeding" pipeline.

**Settings → Switches:**
- Direct Unpack: Off
- Pause Downloading During Post-Processing: On
- Abort jobs that cannot be completed: On
- Action when encrypted RAR is downloaded: Abort
- Download all par2 files: Off
- Post-processing script can fail job: On
- Enable Unpack: On
- Replace illegal characters in Win32 filenames: On

**Settings → Folders:**
- Temporary and Completed folders must be under the same Docker mount root
  as Sonarr/Radarr's import paths (e.g. both under `/data/`). This makes
  *arr's "copy + delete original" a fast same-filesystem move.

**Settings → Special:**
- `ionice` = `-c2 -n7`
- `par2_multicore` = 1

**In Sonarr / Radarr → Settings → Download Clients → SABnzbd → Advanced:**
- Remove Completed Downloads: On
- Remove Failed: On

**In Sonarr / Radarr → Settings → Indexers → Failed Download Handling:**
- Redownload: On
- Remove: On

## Step 4: rotate Sonarr/Radarr API keys

If your previous setup had the keys hard-coded anywhere (e.g. in an old
cleanup script), rotate them now.

- Sonarr → Settings → General → Security → Reset API Key
- Radarr → Settings → General → Security → Reset API Key

Save the new values. You'll paste them into `.cleanup.env` in step 7.

Update Bazarr / Overseerr / Jellyseerr / Notifiarr / etc. with the new keys.

## Step 5: configure and start the containers

Create config directories:

```bash
mkdir -p /mnt/user/appdata/fetcharr/{seedbox-fetcher,sab-watchdog}/state
```

Drop your rclone config and SSH key into the fetcher's config dir:

```bash
cp ~/.config/rclone/rclone.conf  /mnt/user/appdata/fetcharr/seedbox-fetcher/rclone.conf
cp /root/.ssh/id_seedbox         /mnt/user/appdata/fetcharr/seedbox-fetcher/id_seedbox

chown -R 99:100   /mnt/user/appdata/fetcharr/seedbox-fetcher/
chmod 600         /mnt/user/appdata/fetcharr/seedbox-fetcher/id_seedbox
chmod 600         /mnt/user/appdata/fetcharr/seedbox-fetcher/rclone.conf
```

Grab the compose file:

```bash
mkdir -p /mnt/user/appdata/fetcharr
cd /mnt/user/appdata/fetcharr
curl -fsSL https://raw.githubusercontent.com/Cooper8386/fetcharr/main/docker-compose.yml -o docker-compose.yml
```

Bring them up:

**Compose Manager plugin (recommended on Unraid):**

1. Compose Manager → Add Stack → name: `fetcharr`.
2. Point it at `/mnt/user/appdata/fetcharr/docker-compose.yml`.
3. Click "Compose Up".

**Or from SSH:**

```bash
docker compose up -d
```

First run: the fetcher will write a default `config.yml` and exit with an
error. That's expected. Edit the config, then start it again:

```bash
nano /mnt/user/appdata/fetcharr/seedbox-fetcher/config.yml

# Same for the watchdog
nano /mnt/user/appdata/fetcharr/sab-watchdog/config.yml

docker compose up -d
```

Watch the logs:

```bash
docker logs -f seedbox-fetcher
docker logs -f sab-watchdog
```

You should see (fetcher):

```
seedbox-fetcher starting; poll=60s stability=90s routes=2 remote=seedbox
cutoff_mtime: 2026-06-29T09:30:00-05:00
arr_history_check disabled
rclone remote reachable.
state is not bootstrapped; snapshotting current seedbox contents...
bootstrap complete: N releases marked as already-pulled
web UI listening on http://0.0.0.0:8765
```

Open `http://nas-ip:8765` in your browser.

## Step 6: Sonarr / Radarr Remote Path Mapping

Sonarr → Settings → Download Clients → click your qBittorrent client → Edit.

Find the existing Remote Path Mapping (if any) that points at your old
Syncthing-staging path. Update it:

| Field | Value |
|---|---|
| Host | (same as your qBit client host) |
| Remote Path | `/home14/onion8386/downloads/qbittorrent/remote-tv/` |
| Local Path | `/data/torrents/remote-tv/` |

Mirror in Radarr for `remote-movies`.

In **Settings → Download Clients → qBittorrent → Advanced:**
- Remove Completed Downloads: **Off** (the seedbox keeps seeding; the
  cleanup script wipes the NAS staging side after 24h)
- Remove Failed: **On**

## Step 7: install cleanUpDownloadStaging.sh

```bash
cp scripts/cleanUpDownloadStaging.sh /mnt/user/scripts/cleanUpDownloadStaging.sh
chmod 755                            /mnt/user/scripts/cleanUpDownloadStaging.sh

# .env file with rotated keys
cat > /mnt/user/scripts/.cleanup.env <<EOF
SONARR_API_KEY=rotated_sonarr_key
RADARR_API_KEY=rotated_radarr_key
SONARR_URL=http://192.168.5.61:8989
RADARR_URL=http://192.168.5.61:7878
EOF
chmod 600 /mnt/user/scripts/.cleanup.env
```

In User Scripts plugin:
- Add a new script `cleanUpDownloadStaging`.
- Script body:
  ```
  #!/bin/bash
  /mnt/user/scripts/cleanUpDownloadStaging.sh
  ```
- Schedule: **Custom** → `*/30 * * * *`.

Run once with `DRY_RUN=1 /mnt/user/scripts/cleanUpDownloadStaging.sh` first to
see what it would delete without actually deleting anything.

## Step 8: turn off the old setup

After you've watched at least one real release flow through end-to-end:

1. qBit on seedbox → Tools → Options → Downloads → Run external program → uncheck.
2. Syncthing folders for `remote-tv` and `remote-movies` → pause or delete on both ends.
3. Whatever cron was running the old `cleanUpSyncfolders.sh` → remove.

## Verifying the full pipeline

Request something through Overseerr that Sonarr will grab via your tracker.
Then watch:

| Step | Where you see it |
|------|------------------|
| qBit downloads on seedbox | qBittorrent web UI |
| qBit finishes, seeds | qBit "Seeding" state |
| Fetcher notices, waits for stability | `docker logs seedbox-fetcher`, or the UI's release table |
| Fetcher pulls | UI's "Now pulling" section, real-time progress |
| File arrives on NAS | `/mnt/user/data/torrents/remote-tv/<release>/` |
| Sonarr picks it up | Sonarr Activity → Queue |
| Sonarr imports (copy + delete) | `/mnt/user/data/media/tv/<show>/...`, original gone |
| Cleanup wipes any leftovers 24h later | UI logs, cleanup script log |

If any step doesn't happen, the relevant container's logs (or the UI's
Recent Log panel) will tell you why.
