#!/usr/bin/env bash
# shellcheck disable=SC1090,SC2155
#
# cleanUpDownloadStaging.sh
#
# Deletes release folders from the NAS torrent staging directories when:
#   - They are older than $RETENTION_MINUTES (default 1440 = 24h), AND
#   - They are NOT referenced by Sonarr or Radarr's queue or recent grab history.
#
# This script exists because torrent releases on the seedbox are seeded forever
# (managed by Prowlarr), so Sonarr/Radarr never see the download client report
# "stopped" and their native "Remove Completed Downloads" cannot fire. The
# downside is that *arr's import (copy + delete original) leaves the now-empty
# release folder, plus any release that *arr never imported (rejected quality,
# stuck, etc.) is stranded here until this script cleans it up.
#
# Hardening:
#   - Fails CLOSED: if Sonarr OR Radarr API calls fail, no deletions happen.
#   - API keys and URLs come from environment / .env file, not hard-coded.
#   - getProtectedBasenames() logs to STDERR; stdout is a clean basename list.
#   - Compares basenames (handles outputPath being a full path).
#   - Also protects items still in Sonarr/Radarr history as "grabbed".
#   - Single-instance lock via flock.
#   - Optional DRY_RUN=1.
#   - Curl has timeouts.
#
# Usage:
#   DRY_RUN=1 ./cleanUpDownloadStaging.sh    # preview only
#   ./cleanUpDownloadStaging.sh              # real run
#
# Cron (every 30 min):
#   */30 * * * * /mnt/user/scripts/cleanUpDownloadStaging.sh >/dev/null 2>&1
#
# Env file (recommended): /mnt/user/scripts/.cleanup.env  (chmod 600)
#   SONARR_API_KEY=...
#   RADARR_API_KEY=...
#   SONARR_URL=http://192.168.5.61:8989
#   RADARR_URL=http://192.168.5.61:7878
#   HEALTHCHECK_URL=https://hc-ping.com/uuid     (optional)
#

set -uo pipefail

# ---------- Load environment ----------
ENV_FILE="${ENV_FILE:-/mnt/user/scripts/.cleanup.env}"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck source=/dev/null
    set -a; source "$ENV_FILE"; set +a
fi

: "${SONARR_API_KEY:?SONARR_API_KEY is required (set in $ENV_FILE or env)}"
: "${RADARR_API_KEY:?RADARR_API_KEY is required (set in $ENV_FILE or env)}"
: "${SONARR_URL:=http://192.168.5.61:8989}"
: "${RADARR_URL:=http://192.168.5.61:7878}"

# ---------- Configuration ----------
STAGING_FOLDERS=(
    "/mnt/user/data/torrents/remote-tv"
    "/mnt/user/data/torrents/remote-movies"
)

# 24 hours
RETENTION_MINUTES="${RETENTION_MINUTES:-1440}"
RETENTION_HOURS=$(( RETENTION_MINUTES / 60 ))

DRY_RUN="${DRY_RUN:-0}"

LOG_DIR="${LOG_DIR:-/mnt/user/scripts/logs/history}"
TIMESTAMP="$(date +'%Y-%m-%d_%H%M')"
LOG_FILE="$LOG_DIR/cleanup_${TIMESTAMP}.log"

LOCK_FILE="${LOCK_FILE:-/tmp/cleanUpDownloadStaging.lock}"

HEALTHCHECK_URL="${HEALTHCHECK_URL:-}"

mkdir -p "$LOG_DIR"

# ---------- Single-instance lock ----------
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date +'%F %T')] Another instance is running; exiting." >&2
    exit 0
fi

# ---------- Logging ----------
log() {
    local msg="[$(date +'%F %T')] $*"
    printf '%s\n' "$msg" >&2
    printf '%s\n' "$msg" >> "$LOG_FILE"
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || { log "FATAL: required command '$1' not found"; exit 2; }
}
require_cmd curl
require_cmd jq
require_cmd find
require_cmd flock

healthcheck() {
    [[ -z "$HEALTHCHECK_URL" ]] && return 0
    local suffix="${1:-}"
    curl -fsS -m 10 -o /dev/null "${HEALTHCHECK_URL}${suffix}" || true
}

# ---------- API ----------
#
# Print protected BASENAMES (one per line) to STDOUT.
# Return non-zero on ANY API failure so the caller fails closed.
#
# Protect anything currently:
#   - In /api/v3/queue (downloading, importing, completed-pending-import)
#   - Recently grabbed (history eventType=grabbed) and possibly not in queue yet.
#
getProtectedBasenames() {
    local baseUrl="$1"
    local apiKey="$2"
    local appName="$3"

    log "Fetching protected items from $appName ..."

    local response httpCode body
    response=$(curl -fsS -m 30 -w $'\n%{http_code}' \
        -H "X-Api-Key: $apiKey" \
        "$baseUrl/api/v3/queue?pageSize=1000&includeUnknownSeriesItems=true&includeUnknownMovieItems=true" \
        2>/dev/null) || {
        log "ERROR: curl failed talking to $appName queue"
        return 1
    }
    httpCode="${response##*$'\n'}"
    body="${response%$'\n'*}"
    if [[ "$httpCode" != "200" ]]; then
        log "ERROR: $appName queue returned HTTP $httpCode"
        return 1
    fi

    local queueCount
    queueCount=$(jq -r '.totalRecords // (.records | length) // 0' <<<"$body" 2>/dev/null) || queueCount=0
    log "$appName queue: $queueCount records"

    local queueNames
    queueNames=$(jq -r '
        .records // []
        | .[]
        | [ .outputPath, .title, .sourceTitle, .downloadId ]
        | .[]
        | select(type=="string" and length>0 and . != "null")
    ' <<<"$body" 2>/dev/null) || {
        log "ERROR: failed to parse $appName queue JSON"
        return 1
    }

    local hResponse hCode hBody
    hResponse=$(curl -fsS -m 30 -w $'\n%{http_code}' \
        -H "X-Api-Key: $apiKey" \
        "$baseUrl/api/v3/history?pageSize=200&sortKey=date&sortDirection=descending&eventType=1" \
        2>/dev/null) || {
        log "ERROR: curl failed talking to $appName history"
        return 1
    }
    hCode="${hResponse##*$'\n'}"
    hBody="${hResponse%$'\n'*}"
    if [[ "$hCode" != "200" ]]; then
        log "ERROR: $appName history returned HTTP $hCode"
        return 1
    fi

    local historyNames
    historyNames=$(jq -r '
        .records // []
        | .[]
        | select((.eventType == "grabbed") or (.eventType == 1))
        | [ .sourceTitle, .data.downloadClientName, .downloadId ]
        | .[]
        | select(type=="string" and length>0 and . != "null")
    ' <<<"$hBody" 2>/dev/null) || historyNames=""

    {
        printf '%s\n' "$queueNames"
        printf '%s\n' "$historyNames"
    } \
        | sed 's:/*$::' \
        | awk -F/ '{ if (NF>0 && $NF != "") print $NF }' \
        | grep -vE '^$|^null$' \
        | sort -u
}

# ---------- Folder cleanup ----------
cleanFolder() {
    local targetFolder="$1"
    local protectedList="$2"
    local deleted=0 skipped=0 errors=0
    local fileName

    log "Processing folder: $targetFolder (retention ${RETENTION_HOURS}h)"

    while IFS= read -r -d '' filePath; do
        fileName="$(basename "$filePath")"

        if [[ -n "$protectedList" ]] && grep -Fxq -- "$fileName" <<<"$protectedList"; then
            log "SKIP   (queued/recent): $fileName"
            ((skipped++))
            continue
        fi

        if [[ "$DRY_RUN" == "1" ]]; then
            log "DRY-RUN delete: $filePath"
            ((deleted++))
            continue
        fi

        log "DELETE (age >${RETENTION_HOURS}h, not queued): $fileName"
        if rm -rf -- "$filePath"; then
            ((deleted++))
        else
            log "ERROR: rm failed for $filePath"
            ((errors++))
        fi
    done < <(find "$targetFolder" -mindepth 1 -maxdepth 1 \
                  -mmin +"$RETENTION_MINUTES" \
                  -print0)

    log "Summary $targetFolder: deleted=$deleted skipped=$skipped errors=$errors"
    return "$errors"
}

# ---------- Main ----------
log "=== Starting download-staging cleanup (retention=${RETENTION_HOURS}h, dry_run=${DRY_RUN}) ==="
healthcheck "/start"

protectedSonarr=""
protectedRadarr=""

if ! protectedSonarr="$(getProtectedBasenames "$SONARR_URL" "$SONARR_API_KEY" "Sonarr")"; then
    log "ABORT: Sonarr unreachable; refusing to delete (fail-closed)."
    healthcheck "/fail"
    exit 3
fi

if ! protectedRadarr="$(getProtectedBasenames "$RADARR_URL" "$RADARR_API_KEY" "Radarr")"; then
    log "ABORT: Radarr unreachable; refusing to delete (fail-closed)."
    healthcheck "/fail"
    exit 3
fi

allProtected="$(printf '%s\n%s\n' "$protectedSonarr" "$protectedRadarr" | grep -vE '^$' | sort -u)"
protectedCount="$(printf '%s\n' "$allProtected" | grep -cve '^$' || true)"
log "Total protected basenames: $protectedCount"

totalErrors=0
for folder in "${STAGING_FOLDERS[@]}"; do
    if [[ -d "$folder" ]]; then
        cleanFolder "$folder" "$allProtected" || totalErrors=$(( totalErrors + $? ))
    else
        log "WARNING: directory not found: $folder"
    fi
done

# Rotate logs: keep last 30 days
find "$LOG_DIR" -name 'cleanup_*.log' -mtime +30 -delete 2>/dev/null || true

if [[ "$totalErrors" -gt 0 ]]; then
    log "=== Cleanup finished with $totalErrors error(s) ==="
    healthcheck "/fail"
    exit 1
fi

log "=== Cleanup finished ==="
healthcheck ""
