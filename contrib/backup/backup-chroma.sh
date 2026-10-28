#!/usr/bin/env bash
# Back up ChromaDB data from the running container to a timestamped tarball.
#
# Usage:
#   ./backup-chroma.sh [backup_dir]
#
# Defaults to ~/chroma-backups/ if no dir is given.
# Rotates old backups, keeping the last 14 (roughly two weeks of daily backups).
#
# Consistent snapshot (backlog_ca88a083580b): a live `tar` of chroma.sqlite3 +
# HNSW segment files can capture a torn state, or abort with "file changed as
# we read it". So the container is PAUSED only while the data volume is copied
# to a staging volume (~3s for 2.2G on sage), then unpaused and the copy is
# tarred. Clients just wait out the pause; mcp-watchdog needs 2 failed checks
# 30s apart before restarting, so a few seconds never trips it.
#
# Schedule via cron for daily backups:
#   0 3 * * * /path/to/backup-chroma.sh

set -euo pipefail

BACKUP_DIR="${1:-$HOME/chroma-backups}"
CONTAINER_NAME="${CHROMA_CONTAINER:-mcp-chromadb}"
KEEP_BACKUPS=14

mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_FILE="$BACKUP_DIR/chroma-backup-$TIMESTAMP.tar.gz"

echo "Backing up ChromaDB from container '$CONTAINER_NAME' to $BACKUP_FILE..."

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
    echo "ERROR: Container '$CONTAINER_NAME' is not running." >&2
    exit 1
fi

IMAGE=$(docker inspect "$CONTAINER_NAME" --format '{{.Config.Image}}')
DATA_VOLUME=$(docker inspect "$CONTAINER_NAME" \
    --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')
if [ -z "$DATA_VOLUME" ]; then
    echo "ERROR: no named volume mounted at /data in '$CONTAINER_NAME'." >&2
    exit 1
fi
STAGE_VOLUME="chroma-backup-stage-$TIMESTAMP"

cleanup() {
    docker unpause "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker volume rm -f "$STAGE_VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker volume create "$STAGE_VOLUME" >/dev/null
PAUSE_START=$(date +%s)
docker pause "$CONTAINER_NAME" >/dev/null
docker run --rm --entrypoint cp -v "$DATA_VOLUME:/src:ro" -v "$STAGE_VOLUME:/dst" \
    "$IMAGE" -a /src/. /dst/
docker unpause "$CONTAINER_NAME" >/dev/null
echo "Snapshot taken (container paused $(( $(date +%s) - PAUSE_START ))s)"

# Same archive layout as before (top-level data/), streamed so the file is
# owned by the invoking user.
docker run --rm --entrypoint tar -v "$STAGE_VOLUME:/data:ro" "$IMAGE" \
    czf - -C / data > "$BACKUP_FILE"

SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "Backup complete: $BACKUP_FILE ($SIZE)"

# Rotate: keep only the last N backups
COUNT=$(ls -1 "$BACKUP_DIR"/chroma-backup-*.tar.gz 2>/dev/null | wc -l)
if [ "$COUNT" -gt "$KEEP_BACKUPS" ]; then
    REMOVE_COUNT=$((COUNT - KEEP_BACKUPS))
    echo "Rotating: removing $REMOVE_COUNT old backup(s)..."
    ls -1t "$BACKUP_DIR"/chroma-backup-*.tar.gz | tail -n "$REMOVE_COUNT" | xargs rm -v
fi

echo "Total backups: $(ls -1 "$BACKUP_DIR"/chroma-backup-*.tar.gz 2>/dev/null | wc -l)"
