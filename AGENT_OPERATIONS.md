# AGENT_OPERATIONS.md — ongoing operations runbook for AI agents

**You are an AI agent doing ongoing operations on an installed MCP Shared Memory Server.** Read this once and keep it nearby for the session. Pause before any `[CONFIRM]` step. Pause for any unexpected output.

If you're installing the server for the first time, you want `AGENT_INSTALL.md`, not this.

---

## Daily-ops cheat sheet

| Need to do this | Run |
|---|---|
| Check services up | `docker compose ps` |
| Check server health | `curl -s http://localhost:8080/health` |
| Tail server logs | `docker compose logs -f mcp-server` |
| Restart everything | `docker compose restart` (or systemd if managed) |
| Stop everything | `docker compose down` (NEVER add `-v`) |
| Rebuild the image | `docker compose up -d --build mcp-server` |
| Run a smoke test | See "Smoke tests" |
| Check audit log | Mongo `db.audit_log.find().sort({timestamp:-1}).limit(20)` |

`[CONFIRM]` with the human before any restart on a system other agents are actively connected to — restart drops in-process session/subscription state and they'll need to `/go` again.

---

## Restart, four ways — pick the right one

The choice depends on what you changed.

**1. Code change committed in the repo.** Rebuild required.
```bash
docker compose up -d --build mcp-server
```
~10–30 seconds rebuild + restart. Restart drops in-process state.

**2. Env-var change in `.env`.** Recreate containers; no rebuild.
```bash
docker compose up -d
```
Compose detects the env diff and recreates affected containers.

**3. Container hung / health failing / nothing else changed.** Just restart.
```bash
docker compose restart mcp-server
```
Fastest. Same state loss as the others (in-process state lives in the process; restart drops it).

**4. Host reboot or systemd-managed.** If the host is set up with the systemd service (`/etc/systemd/system/mcp-rag-arch.service`), use systemd. Otherwise containers start at boot via `restart: unless-stopped`.
```bash
sudo systemctl restart mcp-rag-arch     # if systemd-managed
sudo systemctl status  mcp-rag-arch     # verify
```
The systemd unit is `Type=oneshot` and calls `start.sh`, which brings up chromadb + mongodb, waits for Chroma health, then brings up mcp-server (skipping rebuild if a cached image exists). ~20 seconds end to end.

**Don't:** `docker compose down -v` (deletes volumes — destroys all data). `docker system prune -a --volumes` (same). `docker volume rm chroma-persistent` or `mcp-mongo-persistent` (catastrophic).

---

## Smoke tests

Three smoke tests live in `contrib/test/`. Run them after any meaningful change. They talk to the real server — they're integration tests, not unit tests.

```bash
# 1. Inbox subscribe + notify, including auth and broadcast fan-out (7 cases)
python3 contrib/test/inbox_resource_smoke.py

# 2. Auth + human-sender bypass (7 cases). Requires an existing user-tier API key.
python3 contrib/test/auth_human_rule_smoke.py

# 3. Stress harness — concurrent sessions, message pile-up. Slower.
python3 contrib/test/mcp_stress_harness.py
```

Pass criterion: each prints PASS lines and exits 0. Any FAIL line, surface to the human with the exact message — these are signal, not noise.

After deploying a code change, run smoke #1 at minimum. After auth-related changes, run #1 + #2.

---

## Backups — verify they're actually running

The backup pipeline (set up by `AGENT_INSTALL.md` Step 8) is three cron jobs writing to local directories + an SSH sync. **Backups run silently. Verify periodically.**

```bash
# Where the local archives land (default paths)
ls -la ~/chroma-backups/ | tail -20
ls -la ~/mongo-backups/  | tail -20

# Recent activity in the backup logs
tail -50 /tmp/chroma-backup.log
tail -50 /tmp/mongo-backup.log
# plus your offsite copy's log, if you set one up

# Cron schedule
crontab -l | grep backup
```

Look for:
- Daily timestamps (3:00 chroma, 3:15 mongo, 4:00 offsite). Gap = cron didn't fire.
- Mongo archive size growing slowly. Sudden drop to a few KB = mongodump is failing silently.
- `~/chroma-backups/` rotated to ~14 files. More than that = rotation broken; less than that on a system that's been up 2+ weeks = some runs failed.

If anything looks wrong, surface to the human with what you saw — do not "fix" cron silently.

---

## Restore drill — DO THIS at least once

**A backup that has never been restored is not a backup.** This is the single most important ongoing-ops task. Run it the first time you take over the system, then ~quarterly.

The drill exercises the chain end-to-end without touching the live data.

```bash
# 1. Pick the most recent mongo backup
LATEST=$(ls -1t ~/mongo-backups/mongo-backup-*.archive.gz | head -1)
echo "Will restore from: $LATEST"

# 2. Spin up a scratch mongo on a different port
docker run -d --name mcp-mongo-restore-test \
    -p 27099:27017 \
    -e MONGO_INITDB_ROOT_USERNAME=test \
    -e MONGO_INITDB_ROOT_PASSWORD=test \
    mongo:7.0
sleep 5

# 3. Restore the archive into the scratch mongo
docker exec -i mcp-mongo-restore-test \
    mongorestore --archive --gzip \
    --username test --password test --authenticationDatabase admin \
    --drop \
    < "$LATEST"

# 4. Spot-check that key collections came back
docker exec mcp-mongo-restore-test \
    mongosh --username test --password test --authenticationDatabase admin \
    --eval 'use mcp_orchestrator; db.registered_functions.countDocuments({})'
docker exec mcp-mongo-restore-test \
    mongosh --username test --password test --authenticationDatabase admin \
    --eval 'use mcp_orchestrator; db.audit_log.countDocuments({})'

# 5. Tear down the scratch container
docker rm -f mcp-mongo-restore-test
```

`[CONFIRM]` the result with the human. Pass criterion: both `countDocuments({})` calls return non-zero numbers that look reasonable (compare to the live system's counts).

**If the drill fails** (mongorestore errors, archive unreadable, container won't start, counts are zero): the backup pipeline is broken and you've just learned that without losing data. Surface immediately. Investigate cron logs, file sizes, and `mongodump` exit codes.

The Chroma backup is a tarball of the volume — restore drill is the same shape but the verify step is "scratch chroma starts and `curl http://localhost:8099/api/v2/heartbeat` returns 200". `[CONFIRM]` with the human before doing the Chroma drill since it's longer to set up.

---

## Debug delivery failures

When an agent says "I sent a message but the recipient never got it," the audit log is your friend.

```javascript
// In mongosh, after `use mcp_orchestrator`:

// Did the send happen?
db.messages.find({_id: "msg_<id>"})

// Did the recipient ever subscribe to their inbox?
db.audit_log.find({event_type: "inbox.subscribe",
                   "details.uri": /<recipient-project>\/<recipient-agent>/})
                  .sort({timestamp: -1}).limit(5)

// Was a subscribe denied?
db.audit_log.find({event_type: "inbox.subscribe.denied"})
            .sort({timestamp: -1}).limit(20)

// Did unsubscribe drop them?
db.audit_log.find({event_type: "inbox.unsubscribe"})
            .sort({timestamp: -1}).limit(20)
```

Common patterns:
- **Send happened, no subscribe ever logged.** Recipient's MCP client isn't subscribing — likely a channel-plugin or client-config issue. Ask the recipient to run their startup macro and check their logs.
- **Subscribe.denied with reason "no active memory session".** Recipient's transport connected but they didn't call `memory_start_session` first. Their startup macro is broken or out of order.
- **Subscribe happened but recipient never got the notification.** Server restart between subscribe and send, or `stateless_http=True` snuck in (check `app.py`). In-process subscriptions don't survive restart.
- **Project-name mismatch.** Subscribe is on `inbox://claude_terminal/main`, send is to `claudeTerminal/main` (camelCase). `helpers.normalize_project` should catch this everywhere; if you see a mismatch, that's a bug — surface it.

The `live_subscribers` field in `memory_send_message` response also tells you, at send time, whether anyone was actually listening. `live_subscribers=0` + `persisted=true` means "stored for next pickup, no live recipient."

---

## Volume-incident recovery

If you suspect the Chroma or Mongo volume has been corrupted, recreated, or wiped:

1. **STOP the server immediately.** Don't make it worse.
   ```bash
   docker compose stop mcp-server
   ```
2. `[CONFIRM]` the suspicion with the human before touching volumes.
3. Verify the actual state:
   ```bash
   docker volume ls | grep -E 'mongo|chroma'
   docker exec mcp-chromadb ls -la /data
   docker exec mcp-mongodb  ls -la /data/db
   ```
   Both should show real files, not just an empty directory.
4. If a volume IS empty but the named volume still exists, do NOT remove it — the data may be recoverable from another mount point or a recent backup.
5. Locate the most recent backup (see "Restore drill" steps for the path).
6. `[CONFIRM]` the recovery plan with the human. Recovery from backup loses all data created after the backup time.
7. Restore: stop the live mongo/chroma container, restore into the live volume from the most recent archive, restart.

This is the script that bit us in 2026-04-03: Chroma 1.x's volume-mount default changed and the existing compose mount was silently ignored, so when the container was recreated, months of data were lost. The compose file in this repo has explicit comments warning against changing the mount path. **Do not change it.**

---

## Watchdog

`watchdog.sh` polls `/health` every 30 seconds and restarts the `mcp-rag-arch` container after 2 consecutive failures. Logs to `/var/log/mcp-watchdog.log`.

```bash
tail -f /var/log/mcp-watchdog.log
```

If the watchdog is restarting the container repeatedly, something is wrong with the server. Stop the watchdog, investigate, and restart it once the underlying issue is fixed:

```bash
# Find the watchdog process
pgrep -af watchdog.sh

# Stop it
sudo systemctl stop mcp-watchdog            # if systemd-managed
# or kill the pid directly with [CONFIRM]
```

`[CONFIRM]` before killing — the human may have started the watchdog manually rather than as a service.

---

## Librarian daemon (optional)

If `ANTHROPIC_API_KEY` is set and the human wants enrichment, `librarian.py` runs on the host (not in Docker) to add signatures, gotchas, and analysis to registered functions. Two modes:

```bash
# Webhook mode — long-running, listens for new function-registration events
python3 librarian.py

# One-shot mode — processes the existing queue and exits
python3 librarian.py --process-queue
```

Check the queue depth:
```
memory_get_enrichment_queue
```

If the queue is growing unboundedly, librarian is down or its API key is wrong. The MCP server keeps working without it — librarian is non-critical.

---

## Authentication ops

If `MCP_AUTH_ENABLED=true`:

```
# Inspect existing keys (owner role required)
memory_admin(action="list_keys")

# Create a new key
memory_admin(action="create_key", name="<who>-<role>", role="agent")
# Copy the api_key value from the response — shown ONCE.

# Revoke a key
memory_admin(action="revoke_key", name="<key-name>")

# Audit log of auth events
memory_admin(action="audit_log", event_type="auth.failed", limit=50)
```

`[CONFIRM]` before revoking a key on a live system — any client using it loses access immediately.

If the human reports "everyone got logged out": check whether `MCP_AUTH_ENABLED` toggled or whether the soft-fallback logic flipped. Check `audit_log` for `auth.failed` and `auth.soft_fallback` events; the latter indicates a client without a key is being downgraded to `agent` tier, which is normal during the soft-auth window.

---

## Common operational surprises

- **`memory_send_message` returns `persisted: true, live_subscribers: 0`.** Not a bug. The recipient isn't connected; the message will be picked up on their next session. If you expected someone live, check their session and subscription state.
- **Tools return `{"error": "no active memory session..."}` randomly after a restart.** Containers restarted; in-process state lost. Affected agents need to call `memory_start_session` again. Their client should auto-reconnect — if it doesn't, that's a known gap (`backlog_0f50effa79af`).
- **A specific project starts returning empty results when others work.** Almost certainly a project-name normalization issue. `helpers.normalize_project` should be applied at every tool entry; a path that bypasses it shows up as a "duplicate project" in `memory_list_projects`. Surface to the human.
- **Mongo volume size growing fast.** The `audit_log` collection has TTL retention but `messages` does not. Look at collection sizes: `db.stats()`. Old completed messages can be pruned manually if needed; ask the human first.
- **Server CPU pegged.** Likely Chroma re-embedding on a query. Most queries are fast; large-batch stores can spike. Wait, then check `docker stats`.

---

## When to escalate to the human

- Any failed restore drill.
- Audit log shows a sustained `auth.failed` rate from an unfamiliar source (potential intrusion attempt).
- Volume corruption, missing files in `/data` or `/data/db`.
- Backups that haven't fired in >36 hours.
- Watchdog has restarted the container >3 times in an hour.
- You're about to do anything destructive — `[CONFIRM]` was a hint.

Report what you observed, what you've already tried, and what you would do next. Don't take destructive action to "fix" something you don't fully understand.
