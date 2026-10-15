#!/usr/bin/env bash
#
# Download a full JSON backup from the app's /export endpoint and keep the
# newest N copies. The JSON round-trips through Admin -> Import.
#
# For a database-level backup, prefer mongodump (see docs/deployment.md).
#
# Configuration (environment variables):
#   BACKUP_URL     Full URL of the export endpoint   (default http://localhost:5052/export)
#   BACKUP_DIR     Where to write backups            (default ./backups)
#   BACKUP_PREFIX  Filename prefix                   (default accounting-backup)
#   KEEP_COUNT     How many backups to keep          (default 7)
#   BACKUP_HEADER  Optional extra HTTP header, e.g. one your SSO proxy accepts
#                  for machine clients: BACKUP_HEADER="Authorization: Bearer ..."
#
# Cron example (daily at 02:00):
#   0 2 * * * BACKUP_DIR=/srv/backups /path/to/backup-accounting.sh >> /var/log/accounting-backup.log 2>&1
#
set -euo pipefail

BACKUP_URL="${BACKUP_URL:-http://localhost:5052/export}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
BACKUP_PREFIX="${BACKUP_PREFIX:-accounting-backup}"
KEEP_COUNT="${KEEP_COUNT:-7}"
BACKUP_HEADER="${BACKUP_HEADER:-}"

mkdir -p "$BACKUP_DIR"
DATE=$(date +%Y-%m-%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/${BACKUP_PREFIX}_${DATE}.json"

echo "$(date): Starting backup from ${BACKUP_URL}..."
CURL_ARGS=(-s -w "%{http_code}" -o "$BACKUP_FILE")
[ -n "$BACKUP_HEADER" ] && CURL_ARGS+=(-H "$BACKUP_HEADER")
HTTP_CODE=$(curl "${CURL_ARGS[@]}" "$BACKUP_URL" || echo 000)

if [ "$HTTP_CODE" -ne 200 ]; then
    echo "$(date): ERROR - Backup failed with HTTP code: $HTTP_CODE"
    rm -f "$BACKUP_FILE"
    exit 1
fi

if [ ! -s "$BACKUP_FILE" ] || ! jq empty "$BACKUP_FILE" 2>/dev/null; then
    echo "$(date): ERROR - Downloaded file is empty or invalid JSON (is a login page in the way?)"
    rm -f "$BACKUP_FILE"
    exit 1
fi

chmod 600 "$BACKUP_FILE"
echo "$(date): Backup successful: $BACKUP_FILE ($(du -h "$BACKUP_FILE" | cut -f1))"

# Retention: keep only the newest KEEP_COUNT backups
ls -1t "${BACKUP_DIR}/${BACKUP_PREFIX}"_*.json 2>/dev/null | tail -n +"$((KEEP_COUNT + 1))" | xargs -r rm -f
echo "$(date): Backup complete."
