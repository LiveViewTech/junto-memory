# junto-backup-verify — sage host-side backup monitor

Daily watchdog that alerts Tom by email if sage's **junto (shared-memory MCP)
backups** go missing, truncated, or stale. junto's persistence is MongoDB +
ChromaDB; these are the crown-jewel store for the whole fleet.

**Owner:** `memory@junto` (host-substrate for sage/junto).
**Runs on:** sage, as user `tlemmons`.
**Origin:** built + fail-tested by `infra-team@nimbus` on 2026-08-21 under
`backlog_d59e8fc1b760`; ownership moved to `memory@junto` on 2026-08-22 when Tom
decoupled sage/junto infra from the Nimbus project. Source was relocated here
from the nimbus infra repo at that point.

## Why it exists

On **2026-08-04** a chroma backup failed **silently** (a `tar` race on the live
SQLite file) and nothing alerted. `memory@junto` owns the backup jobs and the
tar-race root-cause fix; this is the durable **belt-and-suspenders** host-side
monitor that makes a silent failure impossible to miss.

## What it checks (daily 05:00 local, after the backup crons)

Backup crons on sage: chroma **03:00** → mongo **03:15** → sync-to-storm **04:00**.
Verify runs at **05:00** with an hour of slack.

For chroma (`~/chroma-backups`) and mongo (`~/mongo-backups`):
- **Presence** — a matching backup file exists (zero files = ALERT, never a
  vacuous pass; every run logs "checked N of N").
- **Freshness** — newest backup < **26h** old (embedded filename timestamp,
  mtime fallback).
- **Size floor** — chroma ≥ 50 MB, mongo ≥ 20 MB (deliberately well under
  observed ~200 MB / ~72 MB — catches truncation, not drift).
- **Trailing-median** — newest ≥ 50% of the trailing 7-file median (truncation).
- **gzip integrity** — `gzip -t` on the newest (catches present-but-corrupt;
  directly covers the 2026-08-04 shape).

Offsite (**LXC 212 on madrox @ 192.168.15.98**, SFTP-only chroot, dirs
`/data/chroma` + `/data/mongo`, via `~/.ssh/junto-offsite-212`; listed with
sftp, not a remote shell):
- **Freshness** — newest synced copy < 26h. **Receiver unreachable = FAIL**, not
  a clean pass (offsite state UNKNOWN is treated as bad).

> Offsite was repointed 2026-08-22 from the ex-storm Windows host (`.250`,
> physical box decommissioned 08-14) to LXC 212. 212 owns retention (keep-14)
> and the offsite/cloud push; `sync-to-storm.sh` just lands tarballs there.
> `backlog_39b05cf07539`.

Log-signature corroboration is **advisory only** — it never alerts (`/tmp`
clears on reboot, log formats drift), so it can't false-alarm Tom daily.

## Alert path

On any failure the check exits non-zero → systemd `OnFailure=` fires
`junto-backup-verify-alert.service` → runs `junto-backup-verify-alert.sh`, which
POSTs to **Home Assistant**:

`{"title","message","level":"crit","tag":"junto-backup","icon":"mdi:database-alert"}`
→ HAClaude's generic sage→HA alert bus (the same webhook `sage-diskwatch` uses;
URL read from `~/.config/sage-diskwatch/config`, `HA_WEBHOOK`). `crit` →
`notify_critical` (bypasses phone DND); the per-payload `tag` namespaces this
alert so a backup-fail and a disk alert coexist on the phone (no clobber).
LAN-only automation. **HA returns 200 regardless of match — 200 ≠ delivered.**
Contract: HAClaude `msg_b6d18d04831b`.

Full OnFailure chain fail-tested end-to-end 2026-08-22 (forced empty-dir failure
→ dispatcher → HA POST) and **phone-delivery confirmed by Tom**. An email-via-
msmtp fallback (nimbus Zoho sender) rode alongside during the transition and was
removed once HA was verified — completing the decouple from nimbus's mail cred.

Dispatcher log: `~/.local/state/junto-backup/alert.log`.

## Files

| File | Installed to | Notes |
|------|--------------|-------|
| `junto-backup-verify.sh` | `/usr/local/sbin/` | the check, `0755 root` |
| `junto-backup-verify.service` | `/etc/systemd/system/` | oneshot runner, `OnFailure=` alert |
| `junto-backup-verify.timer` | `/etc/systemd/system/` | daily 05:00 local, `Persistent=true` |
| `junto-backup-verify-alert.sh` | `/usr/local/sbin/` | alert dispatcher: POSTs to the HA webhook, `0755 root` |
| `junto-backup-verify-alert.service` | `/etc/systemd/system/` | oneshot that runs the dispatcher |
| `deploy.sh` | — | idempotent installer |

Alerts need `HA_WEBHOOK=` in `~/.config/sage-diskwatch/config` (chmod 600,
shared with sage-diskwatch). Not installed by `deploy.sh` — it's a secret.

## Deploy

```bash
./deploy.sh          # installs artifacts, daemon-reload, enables the timer
# first time only: install ~/.msmtprc from msmtprc.example (chmod 600)
# dry run:  sudo systemctl start junto-backup-verify.service && journalctl -u junto-backup-verify.service -n 40 --no-pager
```

## Known debt / follow-ups (owned by memory@junto)

1. **~~Own mail sender~~ — RESOLVED 2026-08-22.** Instead of standing up a junto
   SMTP sender, alerts now go through HA (phone-verified). The nimbus Zoho mail
   dependency is gone. `backlog_d872ddc3afb2` closed.
2. **Second alert channel (optional).** HA is now the sole channel — if the HA
   VM is down when a backup fails, no alert (same single-channel posture as
   sage-diskwatch). A junto-blocker MCP message is the documented option for a
   2nd channel; needs a shell→MCP send pattern from a systemd context (none
   today). Low priority.
3. **Stale `Documentation=` paths.** `junto-backup-verify.service` and
   `.timer` still point `Documentation=` at the old nimbus Windows path
   (`file:///c/code/Nimbus/...`); the alert service now points here. The two
   committed copies preserve the old path to mirror what's live — fix on their
   next deploy.
