#!/usr/bin/env bash
set -euo pipefail

PUID="${PUID:-99}"
PGID="${PGID:-100}"

# Reuse whatever group already owns PGID; otherwise create one.
if getent group "$PGID" >/dev/null; then
    group_name="$(getent group "$PGID" | cut -d: -f1)"
else
    group_name="fetcher"
    groupadd -g "$PGID" "$group_name"
fi

# Reuse whatever user already owns PUID; otherwise create one.
if getent passwd "$PUID" >/dev/null; then
    user_name="$(getent passwd "$PUID" | cut -d: -f1)"
else
    user_name="fetcher"
    useradd -u "$PUID" -g "$PGID" -d /opt/fetcharr -s /usr/sbin/nologin "$user_name"
fi

mkdir -p /state
chown -R "$PUID:$PGID" /state 2>/dev/null || true

# First-run: drop a sample config if /config is empty.
if [[ ! -f /config/config.yml && -f /opt/fetcharr/app/config.example.yml ]]; then
    cp /opt/fetcharr/app/config.example.yml /config/config.yml
    chown "$PUID:$PGID" /config/config.yml 2>/dev/null || true
    echo "==============================================================" >&2
    echo "fetcharr first run: wrote a default config to /config/config.yml" >&2
    echo "Edit it (especially the rclone remote and routes) and restart." >&2
    echo "Also drop your rclone.conf and SSH key into the same /config dir." >&2
    echo "==============================================================" >&2
fi

if [[ ! -f /config/config.yml ]]; then
    echo "ERROR: /config/config.yml is missing and no example to copy." >&2
    exit 1
fi
if [[ ! -f /config/rclone.conf ]]; then
    echo "ERROR: /config/rclone.conf is missing." >&2
    echo "Generate one on the host with 'rclone config' (remote name: seedbox)," >&2
    echo "then place it at /mnt/user/appdata/seedbox-fetcher/rclone.conf" >&2
    exit 1
fi

export RCLONE_CONFIG="${RCLONE_CONFIG:-/config/rclone.conf}"
export HOME=/opt/fetcharr

exec gosu "$PUID:$PGID" "$@"
