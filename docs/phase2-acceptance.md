# Phase 2 §13 acceptance — transport-drop procedure

The §13 "Phase 2 done" gate from `design:local-first-junto-v0-mvp`. Verifies
that the sync protocol survives a real transport drop with continuous
federated-agent writes, and recovers within ~5 min on reconnect with zero
data loss.

The original spec calls this the "Tailscale-drop" test. The actual transport
is whatever VPN you put between the peer and primary — Tailscale, WireGuard,
or any clean software-switchable link. This procedure assumes Tailscale
because that's what the test harness was first run against.

## What we're testing

| Assertion | How verified |
|-----------|--------------|
| Continuous federated writes during the drop window aren't lost | `federated_writer.py` writes IDs sequentially; post-drop count on primary = total writes attempted minus any explicit transport-fail rows |
| Peer's local op_log accepts writes while primary is unreachable | `memory_sync_pull` on peer shows `peer_origin` next_cursor advancing during drop |
| Primary's op_log unaffected by peer disconnect | `memory_sync_pull` on primary unchanged during drop; sync_observer shows pull_lag growing only |
| Cursor catches up after reconnect | sync_observer logs pull_lag and push_lag returning to 0 within 5 min |
| No duplicate ops produced | post-test mongo aggregation: `op_log` rows grouped by (origin, seq) all have count=1 |
| Vector embeddings remain bit-identical across primary↔peer | post-test embedding compare on a sample of synced learnings; matches |

## Topology

```
xavier (host)
├── VM1: juntoPrimary       Ubuntu Server 24.04, 2 vCPU / 4 GB / 25 GB
│   └── docker-compose.peer.yml   (chromadb + mongodb + mcp-server; sync-engine NOT started)
│       ORIGIN_SERVER_ID=test-primary-1
│       Tailscale-joined
└── VM2: juntoPeer          Ubuntu Server 24.04, 2 vCPU / 4 GB / 25 GB
    └── docker-compose.peer.yml   (chromadb + mongodb + mcp-server + sync-engine)
        ORIGIN_SERVER_ID=peer-xavier-1
        JUNTO_SYNC_PRIMARY_URL=http://<juntoPrimary-tailnet>:8080/mcp
        Tailscale-joined
```

Drop point: `sudo tailscale down` on VM2. `sudo tailscale up` to restore.

## 0. Prerequisites on each VM

On a fresh Ubuntu Server 24.04 VM, before anything else:

```bash
# As root or via sudo:
apt-get update
apt-get install -y docker.io docker-compose-v2 git openssl curl ca-certificates
systemctl enable --now docker

# Add your user to docker group so docker compose works without sudo:
usermod -aG docker $(whoami)
# Then log out + back in so the group takes effect.

# Tailscale (https://tailscale.com/download/linux):
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up   # opens a browser auth flow on first run
```

After Tailscale is up:
```bash
tailscale ip -4   # note this — you'll need both VMs' tailnet IPs
```

Clone the repo on each VM:
```bash
git clone https://github.com/tlemmons/junto-memory.git
cd junto-memory
```

Authorize the controlling Claude's SSH key for unattended setup:
```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
cat >> ~/.ssh/authorized_keys <<'EOF'
<contents of the controller's ~/.ssh/id_ed25519_junto_test.pub>
EOF
chmod 600 ~/.ssh/authorized_keys
```

(That key is `~/.ssh/id_ed25519_junto_test.pub` on the controlling host. Skip this step if
you'll drive the test from xavier directly.)

## 1. Bring up VM1 (juntoPrimary)

```bash
cd ~/junto-memory
ORIGIN_SERVER_ID=test-primary-1 \
  ./contrib/test/bootstrap-primary.sh
```

The script:
- Verifies prereqs.
- Generates `secrets/mongo-keyfile` (mode 400, owned by UID 999).
- Writes `.env` with random Mongo password.
- `docker compose -f docker-compose.peer.yml up -d --build chromadb mongodb mcp-server`.
- Waits for `/health`.
- Issues an admin API key named `peer-xavier-1-sync` and writes it to
  `./peer-credentials.txt`.

When it finishes, `./peer-credentials.txt` contains:

```ini
JUNTO_SYNC_PRIMARY_URL=http://<juntoPrimary-LAN-IP>:8080/mcp
JUNTO_SYNC_PRIMARY_KEY=smk_<...>
ORIGIN_SERVER_ID_REMOTE=test-primary-1
```

The script writes the LAN IP it auto-detected. **Replace the URL host with
juntoPrimary's tailnet name/IP** before copying to VM2 — the test wants
Tailscale as the transport so we can drop it:

```bash
# On VM1, after `tailscale up` and noting the tailnet IP from `tailscale ip -4`:
sed -i "s|http://[0-9.]*:8080/mcp|http://<juntoPrimary-tailnet-ip>:8080/mcp|" peer-credentials.txt
```

Or use the tailnet MagicDNS name if you have it set up:
`http://juntoprimary.<tailnet>.ts.net:8080/mcp`.

## 2. Hand peer-credentials.txt to VM2

`scp` or copy-paste — it's a single short file. On VM2:

```bash
scp <user>@<juntoPrimary-tailnet-ip>:~/junto-memory/peer-credentials.txt ~/junto-memory/
```

## 3. Bring up VM2 (juntoPeer)

```bash
cd ~/junto-memory
source peer-credentials.txt
export JUNTO_SYNC_PRIMARY_URL JUNTO_SYNC_PRIMARY_KEY
ORIGIN_SERVER_ID=peer-xavier-1 \
  ./contrib/test/bootstrap-peer.sh
```

The script:
- Verifies prereqs.
- Probes `${JUNTO_SYNC_PRIMARY_URL%/mcp}/health` — fails loudly if VM1 isn't
  reachable over Tailscale yet. Fix the URL or Tailscale state before
  proceeding.
- Generates this peer's keyfile + initial `.env`.
- Brings up chromadb/mongodb/mcp-server (no sync-engine yet).
- Issues this peer's local admin key + rewrites `.env`.
- Brings up sync-engine, tails its logs for 15s.

Expected log tail:
```
sync_engine: local origin auto-discovered: peer-xavier-1
sync_engine: starting pull loop (primary=http://<juntoPrimary-tailnet>:8080/mcp, interval=10s)
sync_engine: starting push loop (local=http://mcp-server:8080/mcp, interval=5s)
```

## 4. Smoke check both ends

On VM1:
```bash
curl -fsS http://localhost:8080/health
# {"status":"healthy","chroma":"healthy"}
```

On VM2:
```bash
curl -fsS http://localhost:8080/health

# Sync engine smoke (proves HTTPMCPClient + push/pull contract end-to-end):
JUNTO_SYNC_ADMIN_KEY=$(grep ^JUNTO_SYNC_LOCAL_KEY= .env | cut -d= -f2) \
JUNTO_SYNC_LOCAL_URL=http://localhost:8080/mcp \
python3 contrib/test/sync_engine_smoke.py
# Expected: 5/5 PASS.
```

## 5. Start the observer

From any host that can reach both VMs' MCP endpoints (controlling sage, VM1, VM2, or xavier — pick one):

```bash
JUNTO_PRIMARY_URL=http://<juntoPrimary-tailnet>:8080/mcp \
JUNTO_PRIMARY_KEY=<JUNTO_SYNC_PRIMARY_KEY from peer-credentials.txt> \
JUNTO_PEER_URL=http://<juntoPeer-tailnet>:8080/mcp \
JUNTO_PEER_KEY=<JUNTO_SYNC_LOCAL_KEY from VM2's .env> \
python3 contrib/test/sync_observer.py --interval 10 --alert-threshold 50 \
  | tee phase2-acceptance-observer.log
```

You should see per-tick lines with both cursor views and a `pull_lag=0
push_lag=0` (or small) in steady state.

## 6. Start the federated writer

From anywhere that can reach the peer's MCP (typically VM2 itself, or
xavier-host across the LAN):

```bash
JUNTO_PEER_URL=http://<juntoPeer-tailnet>:8080/mcp \
JUNTO_PEER_KEY=<JUNTO_SYNC_LOCAL_KEY from VM2's .env> \
python3 contrib/test/federated_writer.py --rate-per-min 12 \
  | tee phase2-acceptance-writer.log
```

Default rate is 12 writes/min (one every 5s). All writes tagged
`test:phase2-acceptance` for post-test bulk archive.

Watch the observer for 2-3 ticks: with the writer running, the peer's
`peer_origin` next_cursor advances; sync_observer's `push_lag` stays near 0
(writes propagate to primary within ~5s push interval).

## 7. Steady-state baseline (5 min)

Let both run for ~5 minutes before the drop. Confirm in the observer log:
- `pull_lag` ≈ 0
- `push_lag` ≤ 5 (one push-interval's worth of buffered writes)
- No `ALERT:` lines
- `peer_origin` cursor steadily climbing (writer is producing)
- `primary_origin` cursor possibly climbing if anything else is writing on
  the primary (likely flat in this isolated setup)

If steady state isn't clean, **stop here** and debug. Don't run the drop
test against a system that's already drifting.

## 8. Drop the transport

On VM2:
```bash
sudo tailscale down
date +%s  # record drop timestamp
```

The federated writer continues against the peer's local MCP — writes
succeed locally because the peer accepts them into its own op_log; the
sync-engine's push attempts now fail and retry with exponential backoff.

What you should see in real time:
- **federated_writer**: writes keep succeeding (it talks to the peer
  directly, no transport between them).
- **sync_observer**: `peer.next_cursor[peer-xavier-1]` keeps growing.
  `primary.next_cursor[peer-xavier-1]` stays frozen. `push_lag` grows.
- **sync-engine logs** (on VM2): `WARNING` or `ERROR` lines about
  transport timeouts. The 5s→300s exponential backoff is visible.
- **primary** (VM1): no impact. Continues serving any other clients.

Let the drop persist for **30-60 min**. The spec says 1 hour; for the
first dry run 30 min is enough to demonstrate the recovery path.

## 9. Mid-drop check (optional)

While dropped, verify the peer is genuinely accepting and serving writes
locally:

```bash
# On VM2:
curl -fsS http://localhost:8080/health   # still healthy
```

From a federated agent's perspective (Claude session pointed at the peer):
- `memory_query` against tags=test:phase2-acceptance returns recent writes,
  including ones made during the drop.
- `memory_record_learning` succeeds.

## 10. Restore the transport

On VM2:
```bash
sudo tailscale up
date +%s  # record reconnect timestamp
```

Within ~10-30 seconds, sync-engine should detect the connection and begin
draining the push backlog.

**Caveat (known Phase 2 limitation, `backlog_4dd929b6c622`):** HTTPMCPClient
does NOT auto-reconnect — it reuses the same MCP session forever. If the
backoff window is mid-300s sleep when Tailscale returns, recovery is
delayed until the next attempt. Worst case ~5 min extra. If recovery
exceeds 10 min, manually:
```bash
docker compose -f docker-compose.peer.yml restart sync-engine
```

## 11. Verify recovery

Watch sync_observer for the following pass criteria:

| Criterion | Window | Pass condition |
|-----------|--------|----------------|
| Push lag returns to 0 | ≤ 5 min from `tailscale up` | observer's `push_lag` reaches 0 (or small steady-state value) |
| All federated writes present on primary | post-recovery + 30s | total writes counted on primary for origin `peer-xavier-1` equals total writes in federated_writer.log |
| No duplicate ops | post-recovery | mongo aggregation grouping op_log by (origin, seq) shows max(count)=1 |
| Vector embedding parity | post-recovery | sample of synced learnings on both ends produces bit-equal embeddings (`tests/test_chroma_determinism.py` baseline) |

### Quick verification commands

Total writes on primary (origin = peer-xavier-1):
```bash
# On VM1 inside the mongo container:
docker exec mcp-mongodb mongosh --quiet --norc \
  "mongodb://${MONGO_USER}:${MONGO_PASSWORD}@localhost:27017/?directConnection=true&authSource=admin" \
  --eval 'db.getSiblingDB("mcp_orchestrator").op_log.countDocuments({origin: "peer-xavier-1"})'
```

Total writes the federated writer attempted (from its log):
```bash
grep -c 'OK ' phase2-acceptance-writer.log
```

These two numbers should match (within ±1 for any in-flight write at drop
moment).

Duplicate check:
```bash
docker exec mcp-mongodb mongosh --quiet --norc \
  "mongodb://${MONGO_USER}:${MONGO_PASSWORD}@localhost:27017/?directConnection=true&authSource=admin" \
  --eval 'db.getSiblingDB("mcp_orchestrator").op_log.aggregate([
    {$group: {_id: {origin: "$origin", seq: "$seq"}, count: {$sum: 1}}},
    {$match: {count: {$gt: 1}}}
  ]).toArray().length'
# Expected: 0
```

## 12. Stop the harness

```bash
# Stop the writer (Ctrl-C, or via PID):
kill -INT <federated_writer-pid>

# Stop the observer:
kill -INT <sync_observer-pid>
```

Both exit cleanly and print summary lines.

## 13. Cleanup

Test data on the test-primary is throwaway, so the cleanest cleanup is to
nuke the test-primary volumes:

```bash
# On VM1:
docker compose -f docker-compose.peer.yml down -v
```

If you want to keep VM1 running but remove just the test entries:

```python
# From a Claude session with admin tier on the primary:
memory_archive_by_tag(tag="test:phase2-acceptance", project="junto")
```

The peer VM is also disposable:
```bash
# On VM2:
docker compose -f docker-compose.peer.yml down -v
```

When both VMs are destroyed (via VirtualBox), the test leaves no trace
outside `phase2-acceptance-observer.log` and `phase2-acceptance-writer.log`
which live on the controlling host.

## 14. Recording the result

If §13 passes:

1. Bump `design:local-first-junto-v0-mvp` to v0.7.0 with §13 marked complete
   and a brief reference to this run's date + commit SHA.
2. Mark Phase 2 status table row "§13 acceptance" → ✅ done.
3. Bump `architecture:shared-memory-v1` to v1.4.0 covering §4.3 two-track
   instrumentation + sync endpoints + v0.6.0 naming + Phase 2 completion.
4. File a backlog item for any §13 failure modes observed (e.g., if
   HTTPMCPClient auto-reconnect surfaces as a sharp edge — link
   `backlog_4dd929b6c622` to the §13 observation).

If §13 fails:
- Don't claim Phase 2 done. Capture the failure mode as a backlog item.
- Common failure: HTTPMCPClient stuck in long-backoff (see §10 caveat).
  Mitigated by `docker compose restart sync-engine`; structural fix is
  `backlog_4dd929b6c622`.

## Reference

| Artifact | Purpose |
|----------|---------|
| `contrib/test/bootstrap-primary.sh` | VM1 setup |
| `contrib/test/bootstrap-peer.sh` | VM2 setup |
| `contrib/test/federated_writer.py` | Load generator (writes against peer) |
| `contrib/test/sync_observer.py` | Cursor-lag probe (reads both ends) |
| `contrib/test/sync_engine_smoke.py` | Pre-flight transport smoke |
| `docs/peer-deployment.md` | Full peer setup walkthrough (§13 here references this) |
| `design:local-first-junto-v0-mvp` (spec) | §13 architectural rationale |
