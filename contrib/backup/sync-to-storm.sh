#!/usr/bin/env bash
# Sync the latest local junto backups to the OFFSITE receiver.
#
# 2026-08-22: repointed from the ex-storm Windows host (192.168.15.250, physical
# box decommissioned 08-14) to LXC 212 on madrox (192.168.15.98), an SFTP-only
# chrooted receiver (user junto-backup, jail dirs /data/chroma + /data/mongo).
# 212 owns retention (keep-7 per modality since 2026-10-26) AND the offsite/cloud push, so this
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

# Free bytes on the receiver filesystem holding <remote_dir> (sftp `df`, 1K
# blocks, "Avail" column = what a non-root writer can use). Empty on failure.
remote_avail_bytes() {
    printf 'df %s\n' "$1" | sftp_batch 2>/dev/null \
        | awk '$1 ~ /^[0-9]+$/ && NF >= 4 { print $3 * 1024; exit }'
}

# Headroom kept free on the receiver beyond the file itself.
SPACE_MARGIN_BYTES="${SPACE_MARGIN_BYTES:-$(( 100 * 1024 * 1024 ))}"

# Only land backups younger than the receiver's retention (212 prunes
# -mtime +7). Local keeps 14, so without this every pruned file looked "new"
# and was re-uploaded with a fresh mtime, defeating retention (2026-10-27).
OFFSITE_MAX_AGE_DAYS="${OFFSITE_MAX_AGE_DAYS:-7}"

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

    local new=0 skip=0 old=0 f base
    for f in "$local_dir"/$pattern; do
        [ -f "$f" ] || continue
        base=$(basename "$f")
        if [ $(( $(date +%s) - $(stat -c %Y "$f") )) -gt $(( OFFSITE_MAX_AGE_DAYS * 86400 )) ]; then
            old=$(( old + 1 ))
            continue
        fi
        if grep -qxF "$base" <<< "$remote_names"; then
            skip=$(( skip + 1 ))
            continue
        fi
        # Space guard (2026-10-26): a put that hits ENOSPC leaves a .partial
        # filling the receiver, which then also fails the next modality.
        # Skip (and report) instead of starting an upload that cannot fit.
        local size avail
        size=$(stat -c %s "$f")
        avail=$(remote_avail_bytes "$remote_dir")
        if [ -n "$avail" ] && [ $(( size + SPACE_MARGIN_BYTES )) -gt "$avail" ]; then
            fail "${label}: NOT uploading ${base}: needs ${size}B + margin, receiver has ${avail}B free"
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
            # Don't leave a half-written .partial eating receiver space.
            printf 'rm %s/%s.partial\n' "$remote_dir" "$base" | sftp_batch >/dev/null 2>&1 \
                && log "${label}: removed leftover ${base}.partial"
        fi
    done
    log "${label}: ${new} new, ${skip} already present, ${old} older than ${OFFSITE_MAX_AGE_DAYS}d (not sent)"
}

log "=== junto offsite sync -> ${DEST} start ==="
# Mongo first: it is the source of truth and small, so a full receiver
# can't starve it behind a large chroma tarball.
sync_dir "mongo"  "$MONGO_LOCAL_DIR"  "$MONGO_REMOTE"  "mongo-backup-*.archive.gz"
sync_dir "chroma" "$CHROMA_LOCAL_DIR" "$CHROMA_REMOTE" "chroma-backup-*.tar.gz"

# Retention is enforced on the receiver (212, keep-14). No remote rotation here.

if [ "$FAILURES" -gt 0 ]; then
    log "=== offsite sync FINISHED with ${FAILURES} fault(s) ==="
    exit 1
fi
log "=== offsite sync OK (retention owned by receiver) ==="
exit 0
