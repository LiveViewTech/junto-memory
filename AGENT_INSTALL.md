# AGENT_INSTALL.md — install procedure for AI agents

**You are an AI agent installing the MCP Shared Memory Server.** This document is your runbook. Read all of it once before you start. Follow the steps in order. Stop and ask the human before any of the bracketed `[CONFIRM]` decisions, before any destructive command, and any time a step's output doesn't match what's described.

A human is welcome to follow this too — it's just denser than they're used to.

> **Fastest path — use [`junto-stack`](https://github.com/tlemmons/junto-stack) instead.** It wraps this server in a compose file that **self-bootstraps** the Mongo replica set: `docker compose up -d` and you're done, no keyfile or `rs.initiate()` steps. Use THIS document only to run junto-memory standalone from its own repo — it covers the two manual DB-bootstrap steps (Steps 2.5 and 3) that junto-stack automates for you.

---

## What you are installing

A self-hosted MCP server that gives AI coding agents persistent memory and lets multiple agents on the same codebase coordinate. It runs three Docker containers:

- `mcp-rag-arch` — the MCP server (Python, FastMCP, port 8080)
- `mcp-mongodb` — MongoDB 7.0 (knowledge base, sessions, messages, function registry)
- `mcp-chromadb` — ChromaDB 1.x (vector search over the knowledge base)

When install is done the human's AI tools (Claude Code, Cursor, Aider, anything MCP-compatible) connect to `http://<host>:8080/mcp`.

---

## Prerequisites — verify before you start

Run each check. If any fails, stop and ask the human to install or fix it.

```bash
docker --version              # Need 20.10+
docker compose version        # Need v2 (note: space, not hyphen)
curl --version                # Used for health checks
git --version                 # Used to clone
```

The Docker daemon must be running and the current user must be in the `docker` group (or you must use sudo). Confirm:

```bash
docker info >/dev/null 2>&1 && echo "docker ok" || echo "docker NOT ok"
```

Disk: ~2 GB free for images, plus growth for your project's stored knowledge (typically tens of MB per project per month).

Ports needed on the host: `8080` (MCP), `8001` (Chroma), `27019` (Mongo). If any are taken, see "Changing ports" below.

---

## Step 1 — Clone the repository

```bash
cd ~  # or wherever the human prefers
git clone https://github.com/tlemmons/junto-memory.git
cd junto-memory
```

`[CONFIRM]` if the human wants the install rooted somewhere other than `~/junto-memory`.

---

## Step 2 — Configure `.env`

```bash
cp .env.example .env
```

**Open `.env` and set these. Do NOT use the example defaults in production.**

| Variable | What it is | What to set |
|---|---|---|
| `MONGO_USER` | MongoDB root username | Any string. `mcp_orch` is fine. |
| `MONGO_PASSWORD` | MongoDB root password | **Generate a strong password.** `openssl rand -base64 24` works. **Do not leave as `changeme`.** |
| `MONGO_DB` | Default database name | `mcp_orchestrator` is fine. |
| `MONGO_PORT` | Host port for Mongo | Defaults to `27019` in `docker-compose.yml` (NOT `27018` like `.env.example` claims — that's stale). Change only if 27019 is taken. |
| `CHROMA_PORT` | Host port for Chroma | `8001` is fine. |
| `MCP_AUTH_ENABLED` | API-key auth | Leave commented (off) for localhost-only use. **Set to `true` if the server will accept connections from outside this host.** Auth setup happens after first start; see Step 7. |
| `ANTHROPIC_API_KEY` | Only needed if you'll run the librarian enrichment daemon | Leave commented unless the human wants librarian. |

`[CONFIRM]` the password with the human if you generated one — they need to keep it. (1Password, password manager, somewhere durable.)

Ignore the `DB_*_*` external-database section in `.env.example` unless the human asks you to wire up read-only SQL access to their databases. That's an optional feature, not part of base install.

---

## Step 2.5 — Generate the Mongo keyfile

The server runs Mongo as a single-node **replica set** (it needs multi-document transactions + change streams). Replica-set + auth together require an internal-auth **keyfile**, which is deliberately NOT in the repo (it's a secret; `.gitignore` excludes `secrets/`). Generate it before first start:

```bash
mkdir -p secrets
openssl rand -base64 756 > secrets/mongo-keyfile
chmod 400 secrets/mongo-keyfile
sudo chown 999:999 secrets/mongo-keyfile   # 999 = the mongo user inside the container
```

Without this file the `mongodb` container will not start. (junto-stack generates it for you automatically.)

---

## Step 3 — Start the services

```bash
docker compose up -d
```

This builds the `mcp-server` image and starts all three containers. Expect 1–3 minutes the first time (image build + first-run init).

**Then initiate the replica set (one-time, required).** The mongo image starts the daemon with `--replSet rs0` but never runs `rs.initiate()`; until it does there is no primary and every write fails with "No primary exists" — *even though the mongo container's own healthcheck reports healthy*. Run this once (idempotent — safe to re-run):

```bash
set -a; source .env; set +a
docker exec mcp-mongodb mongosh --quiet --norc \
  "mongodb://$MONGO_USER:$MONGO_PASSWORD@localhost:27017/?directConnection=true&authSource=admin" \
  --eval 'try { const s = rs.status(); if (s.ok && s.members && s.members.some(m => m.stateStr === "PRIMARY")) { print("rs0 already initialized"); quit(0); } } catch (e) { const r = rs.initiate({_id:"rs0",members:[{_id:0,host:"mongodb:27017"}]}); if(!r.ok){print("initiate failed: "+JSON.stringify(r));quit(1);} print("rs0 initiated"); } for (let i=0;i<30;i++){ try{ if(db.hello().isWritablePrimary){print("primary elected");quit(0);} }catch(e){} sleep(1000);} print("no primary"); quit(1);'
```

Expect `rs0 initiated` then `primary elected` (or `rs0 already initialized` on a re-run). (junto-stack runs this for you via a one-shot init service.)

While it runs, you can watch logs:

```bash
docker compose logs -f mcp-server
```

When you see `Uvicorn running on http://0.0.0.0:8080`, the server is up. Press `Ctrl-C` to stop tailing logs (the containers keep running — you used `-d`).

---

## Step 4 — Verify health

```bash
curl -s http://localhost:8080/health
```

Expected: `{"status":"healthy","chroma":"healthy","mongo":"healthy",...}`. `"status":"healthy"` means **both** stores are up AND Mongo has a writable primary — so on this standalone path it stays `"degraded"` (HTTP 503, with `"mongo":"no primary..."`) until you've run the `rs.initiate()` step in Step 3. Any non-`healthy` status means something is wrong — see "If health check fails" below.

Also verify all three containers are running:

```bash
docker compose ps
```

Expected: three rows, all with `Up (healthy)` or `Up`. If any is `Restarting`, look at its logs:

```bash
docker compose logs mongodb     # or chromadb, or mcp-server
```

---

## Step 5 — Configure the human's MCP client

Add to `~/.claude.json` (Claude Code, global) **or** to `.mcp.json` in a specific project:

```json
{
  "mcpServers": {
    "junto": {
      "type": "http",
      "url": "http://localhost:8080/mcp"
    }
  }
}
```

For Cursor, add to `.cursor/mcp.json` with the same shape.

For non-localhost installs (other host on the LAN, Tailscale, etc.), replace `localhost` with the host's IP or DNS name. **If the URL is non-localhost, also enable auth (Step 7) — otherwise anyone on the network can read and write the memory store.**

`[CONFIRM]` with the human which MCP client(s) they're using and whether the config should be global or per-project.

After config, the human restarts their MCP client. They should now see the `junto` server with 50+ tools available (`memory_*`).

---

## Step 6 — First-session smoke test

Have the human (or you, if you have access) run an MCP tool to confirm end-to-end:

```
memory_start_session(project="test", claude_instance="install-test",
    role_description="One-shot smoke test agent")
```

Expected: a JSON response with a `session_id`, an empty (or near-empty) `learnings` list, and a `guidelines` block (a handful of server-managed rules; the exact set evolves — don't treat the count as fixed).

If you get an MCP transport error, the client config is wrong. If you get a Python traceback in the response, the server is running but something is broken — capture the traceback and surface it to the human.

---

## Step 7 — (Optional) Enable authentication

**Required if the server is reachable from outside localhost.** Skip if localhost-only and the human accepts the trust model.

1. Stop the server: `docker compose down`
2. Edit `.env`: uncomment `MCP_AUTH_ENABLED=true`
3. Start again: `docker compose up -d`
4. Wait for health (Step 4)
5. Create the first owner-tier key via the MCP tool `memory_admin`:

   ```
   memory_admin(action="create_key", name="<name>-owner", role="owner")
   ```

   The response includes the api_key value. **It is shown once.** The human must store it (1Password, etc.) immediately.

6. Provide the key to the MCP client by adding `"headers": {"X-API-Key": "<value>"}` to the client's MCP config block.

The auth model has four tiers: `owner` (full admin + can create keys), `admin` (manage one project), `user` (human-tier — messages sent from it are forced to chain_depth 0), `agent` (default tier). When `MCP_AUTH_ENABLED=true`, missing keys fall back to `agent` tier (this is "soft auth" — see the architecture spec for the full security posture). Hardening the default beyond soft auth is a separate decision.

---

## Step 8 — (Optional) Enable backups

Backups are NOT enabled by first-time install. Without them, `docker volume rm` or volume corruption permanently loses everything. **Recommend the human enable backups for any non-throwaway install.**

```bash
ls contrib/backup/
# backup-chroma.sh  backup-mongo.sh  sync-to-storm.sh
```

The first two dump local archives to `~/chroma-backups/` and `~/mongo-backups/` with 14-backup rotation. The third syncs them to a remote host over SSH (optional, requires SSH key setup).

To wire them into cron:

```bash
crontab -e
# Add:
0 3 * * * /full/path/to/contrib/backup/backup-chroma.sh
15 3 * * * /full/path/to/contrib/backup/backup-mongo.sh
# (Optional offsite, only if SSH to a backup host is configured)
0 4 * * * /full/path/to/contrib/backup/sync-to-storm.sh
```

`[CONFIRM]` paths with the human — `crontab -e` opens in their `$EDITOR`.

After 24 hours, verify the backups are landing: `ls -la ~/{chroma,mongo}-backups/`.

`[CONFIRM]` whether the human wants to do a restore-drill (recommended). The drill is documented in `AGENT_OPERATIONS.md` — backups that have never been restored are not backups.

---

## Done-when checklist

You can declare the install successful when ALL of these are true:

- [ ] `docker compose ps` shows three containers, all `Up` or `Up (healthy)`.
- [ ] `curl -s http://localhost:8080/health` returns `"status":"healthy"` (both `chroma` and `mongo` healthy — a 503/`degraded` means you skipped `rs.initiate()`).
- [ ] The human's MCP client lists `junto` with 50+ tools.
- [ ] **The real proof:** `memory_start_session(...)` then `memory_record_learning(...)` then read it back with `memory_query(...)` — a persisted write is the authoritative "it works", not the health check.
- [ ] If auth was enabled: the owner key is stored somewhere durable, NOT in the conversation.
- [ ] If non-localhost install: auth is enabled.

Report all six results to the human.

---

## Common failures and how to recover

### Health check fails with connection refused

The server isn't up yet. `docker compose ps` — is `mcp-server` running? If `Restarting`, check its logs.

If logs show `pymongo.errors.ServerSelectionTimeoutError`: the server can't reach a Mongo **primary**. In order of likelihood: (1) you skipped the `rs.initiate()` step (Step 3) — run it; the replica set has no primary until you do. (2) The `mongodb` container isn't `Up` — a missing `secrets/mongo-keyfile` (Step 2.5) stops it; `docker compose ps` and `docker compose logs mongodb`. (3) `MONGO_USER`/`MONGO_PASSWORD` in `.env` don't match what compose sees: `docker compose config | grep MONGO`. (Auth errors surface distinctly as `Authentication failed`, not a selection timeout.)

If logs show `chromadb.errors.ChromaError` or similar: Chroma volume mount is wrong. **This is the bug that caused the April 2026 data loss.** The `docker-compose.yml` mounts the `chroma-data` volume at `/data` inside the container. Do not change the `/data` path (e.g. to `/chroma/chroma`). If you're seeing this on a fresh install with the unchanged compose file, surface to the human — something else is wrong.

### Port already in use

Edit `.env` to set `MONGO_PORT=` or `CHROMA_PORT=` to free ports. The MCP server's host port (8080) is not env-driven — to change it, edit `docker-compose.yml` line `- "8080:8080"`. `[CONFIRM]` port choices with the human; whatever they pick has to match the MCP client config in Step 5.

### "Permission denied" on docker

The user isn't in the `docker` group. Either prefix every command with `sudo`, or `sudo usermod -aG docker $USER` and start a new shell. `[CONFIRM]` with the human before modifying group membership.

### `docker compose up -d` says "version is obsolete"

Old `docker-compose.yml` syntax. The repo's compose file is current; if you see this message, you're either running an old docker compose or have a stray `version:` line. Update docker compose; do NOT add a version line.

### Image build fails

Check the build log output. Most build failures are network-related (apt mirror, pip index). Retry. If it consistently fails on a specific package, surface to the human with the exact line — there may be a transient package issue.

---

## Don'ts

- **Don't change the Chroma volume mount path** (`/data` inside the container). The April 2026 data-loss incident traced to this. Comment in the compose file warns explicitly.
- **Don't set the MongoDB password to `changeme` and leave it.** Even on localhost, this is the default credential and any local process can read everything.
- **Don't skip the health check** (Step 4). "It started without errors" ≠ "it works."
- **Don't enable auth and fail to store the owner key.** The key is shown once. Lose it and you lose owner access (you can recover by deleting the key from Mongo and creating a new one, but that's a manual procedure that involves database surgery).
- **Don't use the `deploy/` folder.** It's vestigial — `deploy/install.sh` references files that aren't in `deploy/` and will fail. Use the root-level `docker-compose.yml` directly.
- **Don't `docker compose down -v`.** That `-v` deletes volumes, which deletes ALL data.
- **Don't deploy to a public-internet IP without auth.** This server is designed for trusted networks (LAN, Tailscale). Putting it on `0.0.0.0` reachable from the internet without auth = anyone reads and modifies the memory store.

---

## What this install does NOT include

These are real ongoing-operations concerns that are NOT part of base install:

- **Systemd auto-start.** `restart: unless-stopped` in compose handles container-level restart, but if the host reboots, Docker may not auto-start. See `AGENT_OPERATIONS.md`.
- **The librarian daemon.** Optional; enriches function-registry entries via Claude Haiku. Requires `ANTHROPIC_API_KEY`. See `AGENT_OPERATIONS.md`.
- **Backup-failure alerting.** Cron writes logs but doesn't page on failure. See `AGENT_OPERATIONS.md`.
- **Restore drills.** Untested backups aren't backups. See `AGENT_OPERATIONS.md`.
- **Multi-host or HA setup.** Single-host only.

---

## When you're done

Report to the human:

1. The six done-when results.
2. Where things were installed (`pwd` of the repo).
3. The MCP URL they should give to other tools.
4. Whether auth is on.
5. Whether backups are scheduled.
6. Anything from the install that surprised you.

Then update any project tracking the human uses (backlog, notes) so the next agent knows the install is done.
