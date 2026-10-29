"""Code-seeded default skills (insert-if-absent, never overwrite).

Why: the global guidelines (trim_12_parking) tell every agent to start a park
with memory_get_skill("parking"), but skills are DATA — a fresh server (or the
isolated work box) has no "parking" skill, so the rule pointed at nothing
(work-box upgrade review, 2026-10-27). Unlike the global-guideline seed, this
one never updates an existing row: each server's parking skill is meant to be
edited locally (sage's carries nimbus-specific lines), and an edit must survive
every boot. Seeded rows are shared (project="") and active.
"""

import logging

log = logging.getLogger(__name__)

SEED_OWNER = "memory"

PARKING_STEPS = """# Parking checklist

Complete ALL items in order:

[ ] 1. FUNCTIONS: Register every function you created or significantly modified this session with memory_register_function (name, file:line, purpose, gotchas). If you modified 3+ files and registered zero functions, your park is INCOMPLETE. If you wrote no functions, say so explicitly.

[ ] 2. LEARNINGS: Answer these three. If any answer is non-empty, call memory_record_learning:
   - "What broke or surprised me?"
   - "What would I warn the next developer about?"
   - "What did I debug for more than 10 minutes?"
   If you learned nothing non-obvious, say so explicitly.

[ ] 3. MESSAGES: Acknowledge any message you read but didn't act on. Leave nothing in "received" limbo across sessions.

[ ] 4. CONTEXT: Use memory_store with topic-scoped titles for substantial context that doesn't fit in learnings or the state spec.

[ ] 5. STATE SPEC: READ your existing spec first (memory_get_spec(name="state:YOUR_NAME")). Carry forward Next Steps you did not work on; merge, never overwrite a bigger session's state. Keep it short. POINTERS, NOT SUMMARIES: learning_/spec_ ids plus one line "pull this before acting on X".

   memory_define_spec(spec_type="agent_state", owner="YOUR_NAME") with these sections (content on the line AFTER each heading):
   ## Current Task
   ## Stopped Because
   ## Status
   ## Files Modified (uncommitted)
   ## Next Steps   (step 1 = the immediate next action on resume)
   ## Blockers
   ## Open Decisions   (what needs deciding · recommendation · what happens if unanswered)
   ## Key Context   (gotchas, hidden constraints, recent decisions)

   The state spec is NEVER empty; if parked clean, write "Parked clean" and why. If the write returns `dropped_standing`, re-add any rule lines that still hold.

[ ] 6. END SESSION: memory_end_session with summary, files_modified, handoff_notes.

[ ] 7. PARKED BANNER: the LAST line of your final reply must be exactly:
   `🅿️ I AM PARKED — /clear then go`
   If you are woken while parked and do NOT resume work, end that reply with the same line.

# When to park (1M-context models)
The park signal is TASK COMPLETION at a clean stopping point, not token count:
- Under ~500K tokens: keep working; don't park mid-task "to preserve context".
- 500–800K: watch for degradation (re-reading known files, re-asking settled questions); park at the next clean stop.
- Above 800K: park even mid-task, with detailed handoff notes.
- If the user says park, park immediately.
- Idle nudge (system notice, kind idle_park_nudge): follow your project's idle policy if the notice carries one; otherwise park if you're at a clean stopping point.
Any park recommendation must cite a token count or a named symptom."""

PARKING_GOTCHAS = (
    "The state spec is the load-bearing artifact and is NEVER empty. The server "
    "blocks a state-spec write that shrinks the existing spec by >50% (intentional; "
    "READ and merge). Registering zero functions after touching 3+ files is the "
    "most-skipped step. Put section content on the line AFTER each '## Heading' — "
    "same-line content parses EMPTY in memory_standup. The banner must be the "
    "literal LAST line. This row was code-seeded (default_skills.py); edit it with "
    "memory_register_skill(project=\"\", name=\"parking\") — the seed never "
    "overwrites an existing row."
)

DEFAULT_SKILLS = [
    {
        "name": "parking",
        "trigger": "when instructed to park (typed `park` macro OR a park-directing message) — step 0 of every park",
        "preconditions": "An active junto session (any project — this skill is SHARED, project=\"\").",
        "steps": PARKING_STEPS,
        "gotchas": PARKING_GOTCHAS,
        "tags": ["parking", "checklist", "shared", "code-seed"],
    },
]


def seed_default_skills(db) -> dict:
    """Insert each DEFAULT_SKILLS entry that has no (project="", name) row.
    Idempotent; never updates. Returns {"inserted": [...], "present": [...]}."""
    from shared_memory.helpers import utc_now_iso
    from shared_memory.tools.skills import _skill_body_hash, _skill_id

    out = {"inserted": [], "present": []}
    if db is None:
        return out
    col = db.skills
    for s in DEFAULT_SKILLS:
        if col.find_one({"project": "", "name": s["name"]}, {"_id": 1}):
            out["present"].append(s["name"])
            continue
        now = utc_now_iso()
        sid = _skill_id("", s["name"])
        col.insert_one({
            "_id": sid, "skill_id": sid, "name": s["name"], "trigger": s["trigger"],
            "steps": s["steps"], "project": "",
            "scope": {"project": "", "role": None, "directory": None},
            "preconditions": s["preconditions"], "gotchas": s["gotchas"],
            "depends_on": [], "tags": s["tags"], "version": "1.0.0",
            "content_hash": _skill_body_hash(s["name"], s["trigger"], s["preconditions"],
                                             s["steps"], s["gotchas"]),
            "status": "active", "pin": False, "owner": SEED_OWNER,
            "created": now, "updated": now, "created_by": "code-seed",
            "updated_by": "code-seed", "confirmed_by": "code-seed", "confirmed_at": now,
        })
        out["inserted"].append(s["name"])
    log.info("seed_default_skills: inserted %s, present %s", out["inserted"], out["present"])
    return out
