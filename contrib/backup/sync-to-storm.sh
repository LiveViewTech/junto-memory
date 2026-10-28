#!/usr/bin/env bash
# Sync the latest local junto backups to the OFFSITE receiver.
#
# 2026-08-22: repointed from the ex-storm Windows host (192.168.15.250, physical
# box decommissioned 08-14) to LXC 212 on madrox (192.168.15.98), an SFTP-only
# chrooted receiver (user junto-backup, jail dirs /data/chroma + /data/mongo).
# 212 owns retention (keep-14 per modality) AND the offsite/cloud push, so this
# script only lands tarballs — no remote rotation, no cloud step here.
# The OFFSITE_* / "storm" names are kept as the offsite-target abstraction.
# Ref: junto/backlog_39b05cf07539, learning (storm decom + VM250 stopgap).
#
# Transport: sftp batch (the receiver is nologin+chrooted — no shell, so no
# scp/ssh remote commands). Atomicity: put to <f>.partial then sftp `rename`,
# so the receiver's freshness/retention never sees a half-written file.
#
# Cron (daily 04:00, after local backups):
#   0 4 * * * /home/tlemmons/sharedUtils/junto/junto-memory/contrib/backup/sync-to-storm.sh >> /tmp/storm-sync.log 2>&1

set -uo pipefail   # NOT -e: aggregate per-file results, report all faults.

# ── Config ──
OFFSITE_HOST="${OFFSITE_HOST:-${STORM_HOST:-192.168.15.98}}"
OFFSITE_USER="${OFFSITE_USER:-${STORM_USER:-junto-backup}}"
OFFSITE_KEY="${OFFSITE_KEY:-${SSH_KEY:-$HOME/.ssh/junto-offsite-212}}"
CHROMA_REMOTE="${CHROMA_REMOTE:-/data/chroma}"
MONGO_REMOTE="${MONGO_REMOTE:-/data/mongo}"

CHROMA_LOCAL_DIR="$HOME/chroma-backups"
MONGO_LOCAL_DIR="$HOME/mongo-backups"

SFTP_OPTS=(-i "$OFFSITE_KEY" -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new)
DEST="${OFFSITE_USER}@${OFFSITE_HOST}"

FAILURES=0
log()  { echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*"; }
fail() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] ERROR: $*" >&2; FAILURES=$(( FAILURES + 1 )); }

# Run a batch of sftp commands on stdin; returns sftp's exit code.
sftp_batch() { sftp "${SFTP_OPTS[@]}" -b - "$DEST"; }

# ── Verify receiver is reachable ──
log "Checking offsite receiver ${DEST} ..."
if ! printf 'pwd\n' | sftp_batch >/dev/null 2>&1; then
    fail "Cannot reach offsite receiver ${DEST}. Skipping offsite sync."
    exit 1
fi

# ── Sync one modality dir ──
# Args: <label> <local_dir> <remote_dir> <pattern>
sync_dir() {
    local label="$1" local_dir="$2" remote_dir="$3" pattern="$4"

    if [ ! -d "$local_dir" ]; then
        fail "${label}: local dir ${local_dir} missing, skipping"
        return
    fi

    # Remote inventory (basenames present), to skip re-uploading full snapshots.
    local remote_names
    remote_names=$(printf 'ls -1 %s\n' "$remote_dir" \
        | sftp "${SFTP_OPTS[@]}" -b - "$DEST" 2>/dev/null \
        | sed -n "s#.*/##p" | grep -E "^${pattern//\*/.*}$" || true)

    local new=0 skip=0 f base
    for f in "$local_dir"/$pattern; do
        [ -f "$f" ] || continue
        base=$(basename "$f")
        if grep -qxF "$base" <<< "$remote_names"; then
            skip=$(( skip + 1 ))
            continue
        fi
        # Atomic: upload to .partial, then rename to the final name.
        if printf 'put %s %s/%s.partial\nrename %s/%s.partial %s/%s\n' \
                "$f" "$remote_dir" "$base" "$remote_dir" "$base" "$remote_dir" "$base" \
            | sftp_batch >/dev/null 2>&1; then
            log "${label}: uploaded ${base} ($(du -h "$f" | cut -f1))"
            new=$(( new + 1 ))
        else
            fail "${label}: upload FAILED for ${base}"
        fi
    done
    log "${label}: ${new} new, ${skip} already present"
}

log "=== junto offsite sync -> ${DEST} start ==="
sync_dir "chroma" "$CHROMA_LOCAL_DIR" "$CHROMA_REMOTE" "chroma-backup-*.tar.gz"
sync_dir "mongo"  "$MONGO_LOCAL_DIR"  "$MONGO_REMOTE"  "mongo-backup-*.archive.gz"

# Retention is enforced on the receiver (212, keep-14). No remote rotation here.

if [ "$FAILURES" -gt 0 ]; then
    log "=== offsite sync FINISHED with ${FAILURES} fault(s) ==="
    exit 1
fi
log "=== offsite sync OK (retention owned by receiver) ==="
exit 0
