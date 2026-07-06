"""Code-defined FLEET DIRECTIVES — cross-server "here's what you need to do" notices.

A directive is a transient, targetable ACTION item surfaced as a banner in the
onboarding bundle (output["directives"]) until the recipient acks it
(memory_ack_directive). Unlike a guideline (always-on POLICY) a directive is a
one-time task that should DISAPPEAR once done; unlike a message it must reach
agents on OTHER servers (the air-gapped work box).

WHY CODE-SEEDED (the load-bearing design choice): messages and DB rows do NOT
federate between junto-memory instances (home sage vs the isolated work box) —
only code that ships in the deploy crosses. So the directive TEXT lives here in
code and is seeded into db.directives on every boot (idempotent, content-aware),
exactly like global_guidelines. Acks are per-server (recorded where the agent
runs) — that's correct: each box tracks its own agents' acknowledgements.

TO ADD/RETIRE A DIRECTIVE: edit FLEET_DIRECTIVES and deploy. Removing one from
code DEACTIVATES its db row on the next boot (it stops surfacing). The durable,
"forever" version of an instruction does NOT belong here — put standing behavior
in a guideline or the launch-contract doc; directives are the transient push.

Directive fields:
  key        stable id (kebab-case). Identity + ack key.
  title      one-line summary (banner headline).
  body       what to do.
  target     {"projects": [...]|None, "agents": [...]|None}. None/empty = all.
             projects match normalized; agents match claude_instance.
  ref        optional spec/doc pointer (e.g. "interface:skill-materialization-v0").
  severity   "action" (owes a do) | "info" (awareness). Banner only; no obligation engine.
  expires_at optional ISO8601 string; past = not surfaced (and seed deactivates).
"""

FLEET_DIRECTIVES = [
    {
        "key": "skill-materialization-rollout",
        "title": "Launcher: materialize active junto skills to .claude/skills before CC boots",
        "body": (
            "design:skill-registry-v0 Phase-2 shipped its SERVER half: "
            "memory_export_skills returns ACTIVE scope-matched skills as ready-to-"
            "write SKILL.md docs {relpath, content}. The LAUNCHER side is now needed "
            "on each box: at agent launch, BEFORE invoking `claude`, call "
            "memory_export_skills and write each payload under <repo>/.claude/skills/"
            "<relpath>, prune stale junto-managed files by the provenance footer "
            "marker `<!-- junto skill ... -->` (never touch hand-authored SKILL.md), "
            "keep it idempotent, and gitignore .claude/skills. Full consumer contract "
            "+ open questions in interface:skill-materialization-v0. This is launcher "
            "work (tlemmons/junto) — coordinate placement with Tom."
        ),
        "target": {"projects": None, "agents": ["coordinator"]},
        "ref": "interface:skill-materialization-v0",
        "severity": "action",
        "expires_at": None,
    },
]


def _directive_targets(target: dict, project: str, agent: str) -> bool:
    """True if a directive's target matches this (project, agent). Empty/None on
    an axis means 'all'. projects compared normalized; agents by instance name."""
    from shared_memory.helpers import normalize_project

    target = target or {}
    projects = target.get("projects")
    if projects:
        norm = normalize_project(project) if project else project
        if norm not in [normalize_project(p) for p in projects]:
            return False
    agents = target.get("agents")
    if agents and agent not in agents:
        return False
    return True


def seed_directives(db) -> dict:
    """Idempotent upsert of FLEET_DIRECTIVES into db.directives.

    Writes a row only when missing or changed (no-change boot = zero writes).
    Stamps updated_by='code-seed'. Code-seeded rows whose key left the code are
    DEACTIVATED (active=False) so retiring a directive in code stops it surfacing
    — but rows NOT created by code-seed are never touched. Returns a summary dict.
    """
    import logging

    from shared_memory.helpers import utc_now_iso

    log = logging.getLogger(__name__)
    if db is None:
        return {"inserted": 0, "updated": 0, "unchanged": 0, "deactivated": 0}

    code_keys = set()
    inserted = updated = unchanged = 0
    now = utc_now_iso()

    for d in FLEET_DIRECTIVES:
        key = d["key"]
        code_keys.add(key)
        desired = {
            "key": key,
            "title": d["title"],
            "body": d["body"],
            "target": d.get("target") or {},
            "ref": d.get("ref"),
            "severity": d.get("severity", "action"),
            "expires_at": d.get("expires_at"),
            "active": True,
        }
        existing = db.directives.find_one({"key": key})
        if existing and all(existing.get(k) == v for k, v in desired.items()):
            unchanged += 1
            continue
        set_fields = dict(desired)
        set_fields["updated"] = now
        set_fields["updated_by"] = "code-seed"
        db.directives.update_one(
            {"key": key},
            {"$set": set_fields,
             "$setOnInsert": {"created": now, "created_by": "code-seed"}},
            upsert=True,
        )
        if existing:
            updated += 1
        else:
            inserted += 1

    # Retire code-seeded rows no longer in code (deactivate, never delete).
    deactivated = 0
    for doc in db.directives.find(
        {"active": True, "created_by": "code-seed"}, {"key": 1}
    ):
        if doc["key"] not in code_keys:
            db.directives.update_one(
                {"key": doc["key"]},
                {"$set": {"active": False, "updated": now, "updated_by": "code-seed"}},
            )
            deactivated += 1

    log.info(
        "seed_directives: %d inserted, %d updated, %d unchanged, %d deactivated",
        inserted, updated, unchanged, deactivated,
    )
    return {"inserted": inserted, "updated": updated,
            "unchanged": unchanged, "deactivated": deactivated}


def get_pending_directives(db, project: str, claude_instance: str) -> list:
    """Active, non-expired, target-matched directives this agent has NOT yet
    acked — as banner headers [{key, title, body, ref, severity}]. Best-effort:
    [] on any failure so it never blocks session start."""
    try:
        if db is None:
            return []
        from shared_memory.helpers import normalize_project, parse_timestamp, utc_now

        now = utc_now()
        out = []
        for d in db.directives.find({"active": True}):
            exp = d.get("expires_at")
            if exp:
                parsed = parse_timestamp(exp)
                if parsed is not None and parsed < now:
                    continue
            if not _directive_targets(d.get("target"), project, claude_instance):
                continue
            norm_project = normalize_project(project) if project else ""
            ack_id = f"{d['key']}:{norm_project}:{claude_instance or ''}"
            if db.directive_acks.find_one({"_id": ack_id}):
                continue
            out.append({
                "key": d["key"],
                "title": d.get("title"),
                "body": d.get("body"),
                "ref": d.get("ref"),
                "severity": d.get("severity", "action"),
            })
        return out
    except Exception:
        return []
