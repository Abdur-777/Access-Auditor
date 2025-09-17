#!/bin/sh
set -euo pipefail

SRC="/var/app/data"
DST="/backups"
STAMP="$(date +%F_%H%M%S)"
ARCHIVE="${DST}/data-${STAMP}.tgz"

mkdir -p "$DST"
tar -czf "$ARCHIVE" -C "$SRC" . || true

# Retention
find "$DST" -type f -name "data-*.tgz" -mtime +${RETENTION_DAYS:-90} -delete
echo "[backup] Wrote ${ARCHIVE} ; pruned >${RETENTION_DAYS:-90}d"
