> **⚠️ Historical (May 2026).** This describes the planned AWS primary + per-laptop
> Hyper-V peer pilot. The work deployment actually runs as a single standalone server,
> spg-junto-central (Tailscale-only, keyed). For upgrading that server, follow the junto
> spec `runbook:workbox-deploy-v0` (v2.x), plus CHANGELOG 1.40.0 "Upgrading from an older build".

# workJunto pilot setup — step-by-step

Purpose: walk operators (initially workClaude on the LVT corp side,
then a couple of additional work humans in the next 1–2 weeks)
through deploying a junto-memory primary/peer pair where:

- **Primary** runs on AWS EC2 with existing live work-project data.
- **Peer** runs in a Hyper-V VM on each user's laptop.
- **Agent (each user's "workClaude") connects to its local peer**,
  not to primary directly. This is the "network-safety" property:
  if Tailscale or AWS drops, the agent keeps working against its
  local peer; writes accumulate and replicate to primary on reconnect.

Empirical basis: as of `3932572` on `main`, junto-memory has passed
two §13 acceptance runs (4-min and 30-min Tailscale drops on a
test VM pair), one cursor-persistence test (sync-engine restart
mid-stream), and one multi-peer test (two peers writing to one
primary, zero collisions, cross-peer fanout verified). See
[peer-deployment.md](./peer-deployment.md) §13 for the harness used.

> **Status:** early-adopter. workJunto is the first non-test
> deployment of the supervisor-pattern sync engine. The supervisor
> has hours of soak, not weeks. Pilot live, but treat regressions
> as a real risk for the first few sessions.

---

## What you need before starting

- [ ] AWS EC2 primary already running junto-memory with the work
      data. (Existing setup; this guide does not stand one up from
      scratch.)
- [ ] Shell access to the primary host as a user who can run `docker`.
- [ ] Tailscale on both the primary host and your laptop, joined to
      the same tailnet. Confirm: from laptop,
      `tailscale ping <primary's-tailnet-hostname>` succeeds.
- [ ] Hyper-V enabled on your laptop, with enough headroom to host
      one Linux VM (recommended: 4 GB RAM, 25 GB disk).
- [ ] A backup of the primary's mongo data volume. The migration in
      step 1 modifies the mongo config; backup is cheap insurance.

---

## Step 1 — Migrate the AWS primary

Follow [primary-migration.md](./primary-migration.md) end-to-end.

The migration covers:

- Verifying or converting mongo to single-node rs0 replica-set mode
  (required for Phase 1 op-log transactions; usually already in
  place if your primary build is from 2026-04 or later).
- Adding `ORIGIN_SERVER_ID=work-central` (or your preferred string)
  to primary's `.env`.
- Minting a per-peer admin-tier API key.
- Confirming peer-reachability over the tailnet.

When you finish, you should have:

```
JUNTO_SYNC_PRIMARY_URL=http://<primary-tailnet-host>:8080/mcp
JUNTO_SYNC_PRIMARY_KEY=smk_...           # admin-tier on primary, for the peer to use
# (You'll need a separate JUNTO_SYNC_LOCAL_KEY per peer, minted later
# by the peer-bootstrap script on each peer host.)
```

Keep these credentials. You'll hand `JUNTO_SYNC_PRIMARY_URL` and
`JUNTO_SYNC_PRIMARY_KEY` to each peer operator.

## Step 2 — Stand up the Hyper-V peer VM

Same shape as the test VMs used to validate the design (see
peer-deployment.md §0):

- Ubuntu Server 24.04 LTS, Generation 2, Secure Boot off (or use the
  "Microsoft UEFI Certificate Authority" template).
- 4 GB static RAM (disable dynamic memory — Hyper-V's default keeps
  guests at ~900 MiB; mongo + chroma + mcp-server + sync-engine
  together don't fit in that).
- 25 GB virtual disk.
- Bridged networking so the VM gets an IP on your LAN.

After install:

```bash
# On the peer VM, as an admin user:
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git curl
sudo usermod -aG docker $USER
# log out + back in so the group takes effect
```

## Step 3 — Join the peer VM to Tailscale

```bash
# On the peer VM:
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
# Authenticate via the URL it prints. Take note of the
# MagicDNS name it assigns (e.g. workjunto-peer-spg.tailnet.ts.net).
```

Verify the peer can reach primary:

```bash
curl -sf http://<primary-tailnet-host>:8080/health
```

Should print `{"status":"healthy",...}`. If not, fix tailnet
connectivity before proceeding — every later step assumes the link
works. If `tailscale ping <primary-host>` works but the curl hangs
or refuses, the fix is **`sudo systemctl restart tailscaled`** on the
side reporting the failure — see Step 8 for the full pattern.

## Step 4 — Clone and configure junto-memory on the peer

```bash
# On the peer VM:
git clone https://github.com/tlemmons/junto-memory.git
cd junto-memory
```

You'll use `docker-compose.peer.yml` (not the default `docker-compose.yml`).
The peer-bootstrap script handles most of the setup; you only need
to seed three values via env:

```bash
export JUNTO_SYNC_PRIMARY_URL=http://<primary-tailnet-host>:8080/mcp
export JUNTO_SYNC_PRIMARY_KEY=smk_...    # from step 1
export ORIGIN_SERVER_ID=peer-<your-name> # pick something descriptive; e.g. peer-workclaude-spg
```

## Step 5 — Run the peer bootstrap script

```bash
# Still on the peer VM, in the junto-memory checkout:
bash contrib/test/bootstrap-peer.sh
```

The script:

1. Writes `.env` with the values you exported.
2. Generates a Mongo keyfile.
3. Brings up `chroma`, `mongo`, `mcp-server` (and waits for mongo
   to become writable via `rs.initiate()`).
4. Mints this peer's local admin API key (`JUNTO_SYNC_LOCAL_KEY`).
5. Brings up `sync-engine`.
6. Tails the engine logs briefly so you can see pull/push start.

**Expected first-30s output:** the engine logs
`sync engine supervisor starting`, then `supervisor: inner engine
starting`, then a series of `httpx HTTP Request: POST … 200 OK`
lines as it begins draining ops from primary. The first pull is
the cold sync — workJunto first-adopter observation (2026-05-20)
was ~3 minutes from an AWS primary with months of accumulated
op-log; budget up to 15–30 minutes for primaries with
substantially larger histories.

Watch progress:

```bash
docker logs -f junto-peer-sync-engine
```

The cold sync is complete when push/pull cadence reaches steady
state (regular `push-send applied=0 deduped=0` or `applied=1 deduped=0`
log lines every ~10 seconds).

## Step 6 — Point the agent at the peer

Your existing workClaude is currently configured to connect to AWS
primary directly. To repoint it at the local peer:

```bash
# Wherever your MCP client config lives (claude.json, .mcp.json,
# Claude Code MCP plugin config, etc.) — replace the primary URL
# with the peer's LOCAL URL:
JUNTO_MEMORY_URL=http://<peer-vm-lan-ip>:8080/mcp
```

You also need to use an API key valid on the peer. The peer's
local admin key (`JUNTO_SYNC_LOCAL_KEY`) works but is admin-tier;
better to mint a user-tier or agent-tier key for the operator's
day-to-day use:

```bash
# On the peer VM:
docker exec junto-peer-mcp-server python -c "
from shared_memory.auth import create_api_key
raw, _ = create_api_key(
    name='workclaude-agent',
    role='user',
    created_by='peer-bootstrap',
)
print(raw)
"
```

Use that key in the agent config.

Restart the agent. From its perspective nothing's changed — it
still calls `memory_record_learning`, `memory_query`, etc. as
before. Reads and writes happen against the local peer; the
sync-engine replicates writes to primary in the background.

## Step 7 — Validate end-to-end

From the agent (workClaude), record a test learning:

```python
memory_record_learning(
    title='workJunto pilot setup verification',
    details='If this learning is visible on primary, the pilot is wired correctly.',
    tags=['pilot-verify', '<your-name>'],
)
```

Wait ~10 seconds, then query from primary directly:

```bash
# On the primary host (AWS EC2):
docker exec <mcp-server-container> mongosh --quiet \
  -u "$MONGO_USER" -p "$MONGO_PASSWORD" --authenticationDatabase admin \
  mcp_orchestrator --eval 'db.learnings.find({tags:"pilot-verify"}).toArray()'
```

If the learning appears, end-to-end replication is working.

## Step 8 — Practice a failure scenario

Before relying on the pilot for live work, exercise the network-loss
path you actually care about:

```bash
# On the peer VM:
sudo tailscale down
# Now have the agent write a learning. Should succeed (local peer
# is still accepting writes).
# Wait a minute. Then:
sudo tailscale up
# Within ~60 seconds, the queued op should land on primary. Verify
# from primary with the same mongosh query as step 7.
```

If the queued write appears on primary after restore, the supervisor
is doing its job. If it doesn't, first verify the link is actually
back: from the peer, `curl -sf http://<primary-tailnet-host>:8080/health`.

**Tailnet gotcha:** if `sudo tailscale up` returned successfully but
the curl still hangs or refuses, the fix is `sudo systemctl restart
tailscaled` on the affected host — **not** another `tailscale down`
followed by `tailscale up`. The down/up loop doesn't recover the
daemon's inbound routing on a host where tailscaled has wedged; the
systemctl restart does. This was a five-wrong-hypothesis session on
the work-side pilot before the pattern was found
(workClaude learning_1b5, 2026-05-20). Use the systemctl-restart as
the first diagnostic step when a peer goes unreachable inbound, not
the last.

Only after the link is verified-back should you capture
`docker logs --tail 100 junto-peer-sync-engine` and reach out
(see Support below).

---

## Operating notes

- **Reboot of the peer VM** — `docker compose -f docker-compose.peer.yml
  up -d` brings the stack back. The cursor file lives in the
  `sync-cursors` named volume and survives across container restarts;
  the sync engine picks up where it left off.
- **Disk space** — chroma vectors plus mongo data grow over time. Plan
  on 5–10 GB after a year of normal-volume use; more if the work
  project generates lots of vector embeddings.
- **Sharing the peer between multiple agents** — supported. Each agent
  uses its own session + identity. The peer's mcp-server is
  per-machine, not per-user.
- **Sharing the peer between multiple humans** — possible but not the
  primary use case. Phase 3 (cross-team scoping) isn't done; if two
  humans share one peer they share the same `tom@claudecontrol`-shaped
  inbox unless they explicitly use different `claude_instance` values.
  For separate humans, prefer one peer per laptop.

## Known limitations (today, not blockers)

- **No agent-side write buffer.** If the agent's *local link to the
  peer* breaks (e.g., laptop loses LAN connectivity to the peer VM —
  rare on a single laptop, but possible if you run the agent on
  device A and the peer VM on device B), the agent sees write
  failures. The peer's supervisor only protects the peer→primary
  link, not the agent→peer link. (`msg_a9ab5c44e1fb`'s "Phase 0
  heartbeat band-aid" is the right next item if this matters.)
- **No multi-machine state-spec ownership.** If you run the same
  agent identity from two laptops simultaneously (e.g.,
  `workclaude@junto` on laptop A and laptop B at the same time),
  state-spec writes race. MVP punts on this; one identity, one
  primary local instance.
- **Cold sync uses the regular pull endpoint.** No optimized
  snapshot transfer. Empirically: ~3 minutes for the first
  workJunto pilot from AWS primary (2026-05-20). Larger primaries
  with year-plus op-log history may take 15–30 minutes.

## Support

- **Issues / bugs** — file at github.com/tlemmons/junto-memory.
- **Cross-project messaging to memory@junto** — send via
  `memory_send_message(to_instance="memory", to_project="junto", ...)`
  from your peer's MCP. Memory will see it next session.
- **Time-sensitive** — tom@lemmons.net out-of-band.

---

## Roll-out plan (for the broader work-side pilot)

Recommended phasing as additional operators come online:

1. **workClaude (first adopter)** — completes steps 1–8 above. If
   anything breaks, fix in the shared repo before the next operator
   starts.
2. **Second work human** — repeats steps 2–8 (skip step 1; the
   primary migration is one-time). Their peer gets its own
   ORIGIN_SERVER_ID and admin key.
3. **Third+ operator** — same as step 2.

If three peers are running simultaneously and steady-state stays
healthy for a week, junto-memory is ready to call v1 for the work
deployment. Sage-side observations should mirror that: the
`memory_query` cross-project results work consistently, no agent
has reported a sync failure, and no operator has needed to manually
restart sync-engine.
