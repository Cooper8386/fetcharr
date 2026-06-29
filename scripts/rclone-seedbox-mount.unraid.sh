#!/bin/bash
#
# Unraid User Scripts variant of the seedbox rclone mount.
# Set the script schedule to "At Startup of Array" in the Unraid User Scripts plugin.
#
# Unraid does not ship systemd. This script mounts seedbox SFTP via rclone in
# the background, and a companion 'check' invocation can be scheduled every
# few minutes to remount if the mount disappears.

set -euo pipefail

MOUNT_POINT="/mnt/seedbox"
REMOTE="seedbox:/home14/onion8386"
RCLONE_CONF="/boot/config/plugins/rclone/.rclone.conf"
LOG_FILE="/var/log/rclone-seedbox.log"

mkdir -p "$MOUNT_POINT"

# If already mounted and responsive, exit.
if mountpoint -q "$MOUNT_POINT" && timeout 5 ls -1 "$MOUNT_POINT" >/dev/null 2>&1; then
    echo "rclone mount already healthy at $MOUNT_POINT"
    exit 0
fi

# Tear down a stale mount if any.
fusermount -uz "$MOUNT_POINT" 2>/dev/null || true

# Wait briefly for network to be up before mounting.
for _ in $(seq 1 30); do
    if ping -c1 -W1 1.1.1.1 >/dev/null 2>&1; then break; fi
    sleep 1
done

nohup /usr/bin/rclone mount "$REMOTE" "$MOUNT_POINT" \
    --config "$RCLONE_CONF" \
    --allow-other \
    --read-only \
    --vfs-cache-mode minimal \
    --dir-cache-time 30s \
    --poll-interval 30s \
    --attr-timeout 30s \
    --buffer-size 64M \
    --log-file "$LOG_FILE" \
    --log-level INFO \
    >/dev/null 2>&1 &

# Wait for the mount to come up before exiting so dependent containers start cleanly.
for _ in $(seq 1 30); do
    if mountpoint -q "$MOUNT_POINT"; then
        echo "rclone mount established at $MOUNT_POINT"
        exit 0
    fi
    sleep 1
done

echo "ERROR: rclone mount did not come up at $MOUNT_POINT" >&2
exit 1
