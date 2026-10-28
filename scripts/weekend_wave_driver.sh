#!/usr/bin/env bash
# Weekend facet-wave driver (2026-07-24..26) — cron-fired, self-throttling.
#
# Each fire: check the usage meter, and if there is headroom run up to $BATCH
# slices headless on Fable (COMMIT mode per scripts/facets_wave_instructions.txt).
# Slices are idempotent (needs_extraction + reviewed-row preservation), so any
# interruption — 5-hour window trip, kill, reboot — costs nothing: the next
# fire resumes where things stopped.
#
# Stop conditions (checked in order):
#   - stopfile present (manual abort: touch $STOPFILE)
#   - COMPLETE marker present (all slices done)
#   - usage endpoint unreachable  -> fail-safe: spend nothing this fire
#   - seven_day >= WEEKLY_CAP     -> permanent stop (writes stopfile)
#   - five_hour >= FIVE_HOUR_CAP  -> skip this fire, resume after window reset
set -u

REPO=/home/tlemmons/sharedUtils/junto/junto-memory
SLICES=$REPO/data/wave_slices_2026-07-24
DONE=$SLICES/done
OUT=$REPO/data/wave_out_2026-07-24
LOG=/home/tlemmons/junto-logs/weekend-waves.log
STOPFILE=$SLICES/STOP
COMPLETE=$SLICES/COMPLETE
LOCK=/tmp/weekend_wave_driver.lock

WEEKLY_CAP=70        # Tom-approved hard cap (2026-07-23)
FIVE_HOUR_CAP=85
BATCH=${BATCH:-2}    # slices per fire, sequential (env-overridable for tests)
SLICE_TIMEOUT=1800   # seconds per headless slice run
MODEL=claude-fable-5

# cron runs with a minimal PATH — claude and node live in user dirs
export PATH=/home/tlemmons/.local/bin:/home/tlemmons/.nvm/versions/node/v20.19.5/bin:/usr/local/bin:/usr/bin:/bin

mkdir -p "$DONE" "$OUT" "$(dirname "$LOG")"
exec 9>"$LOCK"
flock -n 9 || exit 0          # previous fire still running

log() { echo "[$(date -u +%FT%TZ)] $*" >>"$LOG"; }

[ -f "$STOPFILE" ] && exit 0
[ -f "$COMPLETE" ] && exit 0

USAGE=$(python3 "$REPO/scripts/check_usage.py") || { log "usage check FAILED — spending nothing"; exit 0; }
eval "$USAGE"                  # sets FIVE_HOUR, SEVEN_DAY
log "meter: five_hour=${FIVE_HOUR}% seven_day=${SEVEN_DAY}%"

if [ "$SEVEN_DAY" -ge "$WEEKLY_CAP" ]; then
    log "WEEKLY CAP ${WEEKLY_CAP}% reached — permanent stop"
    touch "$STOPFILE"
    exit 0
fi
if [ "$FIVE_HOUR" -ge "$FIVE_HOUR_CAP" ]; then
    log "five-hour window at ${FIVE_HOUR}% — waiting for reset"
    exit 0
fi

ran=0
for slice in "$SLICES"/bulk_*.json; do
    [ -e "$slice" ] || break
    [ "$ran" -ge "$BATCH" ] && break
    name=$(basename "$slice" .json)
    log "running $name (model $MODEL)"
    prompt="You are a facet extraction wave agent. Read your full instructions at $REPO/scripts/facets_wave_instructions.txt FIRST and follow them exactly. Slice file: $slice (JSON: {ids:[...], pairs:[[a,b,sim],...]}). This is COMMIT mode. Fetch each doc's title+content from Chroma (localhost:8001, python chromadb; search the proj_*/shared_* collections by id). Write facet rows to Mongo learning_facets per the COMMIT-mode stamping rules in the instructions (mongo localhost:27019, creds in $REPO/.env, authSource=admin, db mcp_orchestrator) AND append the same rows as JSONL to $OUT/$name.rows.jsonl. Judge the slice's pairs list and append verdicts to $OUT/$name.verdicts.jsonl. Do not use MCP tools; work via python against chroma/mongo directly. Final output: one line of counts."
    if timeout "$SLICE_TIMEOUT" claude -p "$prompt" \
         --model "$MODEL" --dangerously-skip-permissions \
         >>"$OUT/$name.result.txt" 2>>"$OUT/$name.err.txt"; then
        mv "$slice" "$DONE/"
        log "$name DONE"
        ran=$((ran+1))
    else
        rc=$?
        log "$name FAILED rc=$rc (left in place for retry)"
        if grep -qi "limit\|rate" "$OUT/$name.err.txt" 2>/dev/null; then
            log "looks like a usage limit — ending this fire"
        fi
        break                  # any failure ends the fire; next cron retries
    fi
done

remaining=$(ls "$SLICES"/bulk_*.json 2>/dev/null | wc -l)
log "fire done: ran=$ran remaining=$remaining"

if [ "$remaining" -eq 0 ]; then
    touch "$COMPLETE"
    log "ALL SLICES COMPLETE"
    claude -p "Call memory_start_session(project='junto', claude_instance='memory', task_description='weekend wave completion report') then memory_send_message(to_instance='memory', to_project='junto', category='info', subject='Weekend facet waves COMPLETE', message='All slices in data/wave_slices_2026-07-24 processed. Outputs in data/wave_out_2026-07-24; log ~/junto-logs/weekend-waves.log.') then memory_end_session(summary='wave completion report sent')." \
        --model "$MODEL" --dangerously-skip-permissions \
        >>"$LOG" 2>&1 || log "completion report failed (non-fatal)"
fi
