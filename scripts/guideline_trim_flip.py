#!/usr/bin/env python3
"""Flip the GLOBAL guideline scope to the design:guideline-trim-v0 0.2.0 block.

Tom-approved 2026-07-28; coordinator@nimbus approved 0.2.0 (msg_a273eb97c526).
Run AFTER the 1.38.0 server deploy (the new block's [7]/[10]/[12] point at
tool descriptions and the shared `parking` skill that ship with it).

What it does (in order, single run, idempotent via the version guard):
  1. Copies every current scope="global" guideline to `guidelines_archive`
     (with archived_at) and sets active=False on the original.
  2. Inserts the 12 new global rules (names trim_01..trim_12, priority 10..21).
  3. Bumps guidelines_meta version via bump_guidelines_version.
Project scopes (nimbus/sage/emailtriage) are NOT touched.

Usage: guideline_trim_flip.py [--dry-run]   (default is dry-run: pass --execute)
Rollback: restore from guidelines_archive (flip active flags back) + bump version.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from facets_backfill import _env  # noqa: E402

NEW_RULES = [
    ("trim_01_accuracy_over_agreement", 10, "ACCURACY OVER AGREEMENT — identify the strongest reasons an approach might fail BEFORE assessing it; agreement is earned through analysis, never given by default. After proposing any solution or design decision, state the risks, what you are unsure of, and what you are assuming. State uncertainty explicitly (\"I think X but haven't verified Y\" beats \"this works\"). If you realize something you said was wrong or incomplete, say so immediately — don't wait to be caught. About to affirm? Verify you actually analyzed — and if it IS right, explain why it is right rather than affirming it."),
    ("trim_02_memory_first_at_the_artifact", 11, "MEMORY FIRST — AT THE ARTIFACT. Call memory_query and/or memory_find_function at these five moments. Every trigger is an observable action — a thing you can watch yourself doing — NEVER a feeling of uncertainty:\n(1) ABOUT TO RUN: before executing a build, SQL, deploy, or config command — the exact procedure is likely recorded.\n(2) ABOUT TO SEND: before sending a message that asserts a factual claim about system behavior or state you did not verify THIS session — query the claim's topic first. Asides and rationale sentences count the same as conclusions.\n(3) ABOUT TO PROBE: before empirically probing a DESIGNED subsystem to explain its behavior (SSH, logs, DB, grep) — pull its design and prior learnings first. Live symptoms are ambiguous without the design in hand; \"reading reality\" is not exempt.\n(4) ABOUT TO RECORD: before recording a learning that contradicts, supersedes, or surprises — query first.\n(5) ABOUT TO ASK: before asking the user for build steps, credentials, paths, or process — they are probably recorded.\n(Why these trigger shapes: see the 2026-07-20 rework note in the archived mandatory_memory_query rule — artifact-fired rules get followed; absence-detection rules do not. Do not re-add the merged memory_first_designed_system rule separately.)"),
    ("trim_03_execute_dont_ask", 12, "EXECUTE, DON'T ASK — after alignment, do the work and report results. User approval is required ONLY for: (1) database writes, (2) deploys to staging/production, (3) irreversible production changes, (4) git push to shared branches. Everything else: do it, show the result. Ask only when genuinely blocked."),
    ("trim_04_knowledge_to_server", 13, "PERSISTENT KNOWLEDGE GOES TO THE SERVER — record_learning / store / define_spec / register_function; never to self-made local files (invisible to other agents, lost on repo switch). CC's built-in auto-memory is separate, per-machine, and must never hold anything another agent needs."),
    ("trim_05_record_immediately", 14, "RECORD LEARNINGS IMMEDIATELY (not at park) on: non-obvious root cause, data-model quirk, deploy/config gotcha, workaround, contradicted assumption, undocumented behavior, race/timing issue, any >10-minute debug."),
    ("trim_06_session_discipline", 15, "SESSION DISCIPLINE — after start: check your backlog and messages. Before ending incomplete work: backlog item with next steps OR detailed handoff notes. Never disappear mid-task."),
    ("trim_07_messages_mark_as_seen", 16, "MESSAGES ARE MARK-AS-SEEN — get_messages advances your read watermark; returned messages will not reappear. For every message read: act, reply, acknowledge, or carry it explicitly. include_seen=true for full-window catch-up. Don't peek at messages right before ending a session unless you'll disposition what comes back. (Send-side rules: see memory_send_message's description.)"),
    ("trim_08_pointers_not_summaries", 17, "POINTERS, NOT SUMMARIES — state specs and handoffs carry pointers to authoritative entries (learning_/spec_ ids + one line \"pull this before acting on X\"), never paraphrases; a summary creates false sufficiency. REFRESH YOUR STATE SPEC AT EVERY CLEAN STOPPING POINT, parked or not — sessions can die mid-tool-call and the state spec is the only recovery anchor."),
    ("trim_09_contracts_before_code", 18, "CONTRACTS BEFORE CODE — any boundary another agent consumes gets a spec (define_spec type=interface) + consumer notification BEFORE implementation. Never build both sides of a boundary you don't own."),
    ("trim_10_check_freshness", 19, "CHECK FRESHNESS — check the age field on every memory result; verify anything >30 days before trusting; supersede outdated entries you own; route others' stale docs to their project coordinator. (Mechanics: memory_query's description.)"),
    ("trim_11_concise_output", 20, "CONCISE OUTPUT — lead with what you did or need; no process narration; a 3-line result beats a 30-line explanation."),
    ("trim_12_parking", 21, "PARKING — when you are instructed to park (the user types `park`, OR a directive/message tells you to park), step 0 is memory_get_skill(\"parking\") — the checklist and context-band guidance live there. Never end_session without a current state spec."),
    ("trim_13_read_the_source", 22, "READ THE SOURCE — about to assert or ADOPT a fact you got from a derived view? Derived views include: summaries, state specs, handoffs, formatted tool output (git porcelain), figures you computed from one, and other agents' reports or corrections — including well-evidenced ones (check the claim you're ADOPTING, not just the claim being corrected). Verify against the source of record — never by reading a SECOND derived view; the failure shape is \"checked one derived view and stopped.\" One derived artifact is a pointer, not a proof. (Read-side companion of POINTERS, NOT SUMMARIES.)"),
]


def main():
    execute = "--execute" in sys.argv
    from pymongo import MongoClient
    env = _env()
    c = MongoClient("localhost", int(env.get("MONGO_PORT", 27019)),
                    username=env["MONGO_USER"], password=env["MONGO_PASSWORD"],
                    authSource="admin", directConnection=True)
    db = c[env.get("MONGO_DB", "mcp_orchestrator")]

    if "--rollback" in sys.argv:
        # One-command restore: reactivate the archived pre-trim block,
        # deactivate trim_*, bump version. Agents revert at next attach.
        trims = list(db.guidelines.find({"scope": "global", "active": True,
                                         "name": {"$regex": "^trim_"}}))
        olds = list(db.guidelines.find({"scope": "global", "active": False,
                                        "deactivated_by": "guideline_trim_flip"}))
        print(f"{'EXECUTE' if execute else 'DRY-RUN'} ROLLBACK: deactivate "
              f"{len(trims)} trim_* rules, reactivate {len(olds)} archived rules.")
        if not olds:
            print("ABORT: nothing to restore.")
            return 1
        if not execute:
            print("dry-run only — rerun with --rollback --execute")
            return 0
        from shared_memory.helpers import utc_now
        from shared_memory.tools.guidelines import bump_guidelines_version
        now = utc_now()
        for g in trims:
            db.guidelines.update_one({"_id": g["_id"]}, {"$set": {
                "active": False, "deactivated_by": "guideline_trim_rollback",
                "deactivated_at": now}})
        for g in olds:
            db.guidelines.update_one({"_id": g["_id"]}, {"$set": {"active": True},
                "$unset": {"deactivated_by": "", "deactivated_at": "", "note": ""}})
        v = bump_guidelines_version(db, "guideline_trim_rollback")
        print(f"ROLLED BACK. guidelines_version -> {v}. Agents revert at next session start.")
        return 0

    current = list(db.guidelines.find({"scope": "global", "active": True}))
    already = [g for g in current if g["name"].startswith("trim_")]
    if already:
        print(f"ABORT: {len(already)} trim_* rules already active — flip appears done.")
        return 1
    print(f"{'EXECUTE' if execute else 'DRY-RUN'}: archiving {len(current)} global rules, inserting {len(NEW_RULES)}.")
    for g in current:
        print(f"  archive: {g['name']}")
    for name, _, rule in NEW_RULES:
        print(f"  insert:  {name} ({len(rule.split())}w)")
    total = sum(len(r.split()) for _, _, r in NEW_RULES)
    print(f"new global block: {total} words")
    if not execute:
        print("dry-run only — rerun with --execute")
        return 0

    from shared_memory.helpers import utc_now
    from shared_memory.tools.guidelines import bump_guidelines_version
    now = utc_now()
    for g in current:
        db.guidelines_archive.insert_one({**g, "archived_at": now,
                                          "archived_by": "guideline_trim_flip"})
        db.guidelines.update_one({"_id": g["_id"]}, {"$set": {
            "active": False, "deactivated_by": "guideline_trim_flip",
            "deactivated_at": now,
            "note": "relocated/compressed per design:guideline-trim-v0 0.2.0"}})
    for name, priority, rule in NEW_RULES:
        db.guidelines.update_one(
            {"name": name},
            {"$set": {"name": name, "scope": "global", "rule": rule,
                      "priority": priority, "active": True,
                      "created": now, "created_by": "guideline_trim_flip"}},
            upsert=True)
    v = bump_guidelines_version(db, "guideline_trim_flip")
    print(f"DONE. guidelines_version -> {v}. Agents adopt at next session start.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
