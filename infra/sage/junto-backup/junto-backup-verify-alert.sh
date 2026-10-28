#!/usr/bin/env bash
# Alert dispatcher for junto-backup-verify.service failures (systemd OnFailure=).
#
# CHANNEL: Home Assistant webhook — HAClaude's generic sage->HA alert bus
#   (the same webhook sage-diskwatch uses). Per-payload tag namespaces this
#   alert so a backup-fail and a disk alert coexist on Tom's phone (no clobber).
#   LEVEL=warn (NOT crit): warn -> notify_important = a single push, NO repeat
#   loop, no DND bypass. crit is a REPEAT-UNTIL-ACKNOWLEDGED loop (re-buzzes every
#   ~5min until the phone ACK button) reserved for life-safety — a backup failure
#   doesn't need that, and a stale crit self-perpetuates until acked (incident
#   2026-08-22, learning_09092a781d2ea070). The monitor's same-tag "recovered"
#   info post then cleanly REPLACES this warn card. HA returns 200 regardless of
#   match, so 200 != delivered. Contract: HAClaude msg_b6d18d04831b / msg_01d8ef3a5d99.
#
# History: an email-via-msmtp fallback (reusing the nimbus Zoho sender) rode
# alongside HA during the transition; removed 2026-08-22 once the HA path was
# phone-verified, completing the decouple from nimbus's mail credential. To add
# a second channel later, a junto-blocker MCP message is the documented option
# (needs a shell->MCP send pattern from systemd; junto/backlog_d872ddc3afb2).
#
# Runs as user tlemmons via junto-backup-verify-alert.service. Explicit paths
# (not $HOME) so it behaves identically under systemd.
set -uo pipefail

HA_CONFIG=/home/tlemmons/.config/sage-diskwatch/config   # provides HA_WEBHOOK (chmod 600, shared)
LOG=/home/tlemmons/.local/state/junto-backup/alert.log
mkdir -p "$(dirname "$LOG")"
ts=$(date -u +%FT%TZ)
host=$(hostname)

# ---- Build the message from the failed run's journal ----
faults=$(journalctl -u junto-backup-verify.service -n 40 --no-pager 2>/dev/null \
    | grep -E 'ERROR|FAIL|STALE|UNDERSIZE|ZERO|integrity|missing' | tail -12)
[ -z "$faults" ] && faults="(no parsed fault lines — see full journal)"
msg="junto backup verification FAILED on ${host} at ${ts}.

${faults}

One of: a daily junto backup (chroma/mongo) is MISSING, TRUNCATED, or the offsite
(storm) copy is STALE/unreachable. Backups: chroma ~/chroma-backups (03:00), mongo
~/mongo-backups (03:15), sync-to-storm 04:00. junto Mongo+Chroma = shared-memory MCP
store. Full journal: journalctl -u junto-backup-verify.service -n 80 --no-pager"

# ---- PRIMARY: HA webhook ----
HA_WEBHOOK=""
# shellcheck disable=SC1090
[ -f "$HA_CONFIG" ] && source "$HA_CONFIG"
if [ -n "$HA_WEBHOOK" ]; then
    jenc() { printf '%s' "$1" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))'; }
    payload=$(printf '{"title":%s,"message":%s,"level":"warn","tag":"junto-backup","icon":"mdi:database-alert"}' \
        "$(jenc "sage junto-backup FAILED")" "$(jenc "$msg")")
    if curl -fsS -m 10 -X POST -H "Content-Type: application/json" -d "$payload" "$HA_WEBHOOK" >/dev/null 2>&1; then
        echo "$ts HA alert POSTed (HTTP ok; 200!=delivered)" >> "$LOG"
    else
        echo "$ts HA alert POST FAILED (HA unreachable?)" >> "$LOG"
    fi
else
    echo "$ts NO HA_WEBHOOK in $HA_CONFIG — HA alert skipped" >> "$LOG"
fi

exit 0
