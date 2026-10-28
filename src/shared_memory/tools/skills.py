"""Skill registry tools — reusable, task-invoked how-to procedures.

design:skill-registry-v0 (owner: memory). A *skill* is executable knowledge:
a procedure an agent reaches for at the moment of doing a repeatable task
(run the eval gate, bring up the dev env, deploy-greenlight). It is distinct
from the three existing knowledge shapes:

    guideline = always-on POLICY        (every turn)
    macro     = startup RITUAL          (go / park)
    skill     = TASK-invoked PROCEDURE  (load-on-task)   <-- this module

THE design headline (both peer reviews, convergent): AUTHORING is the critical
path, and it is a TRUST problem — you EXECUTE a skill, so a plausible-but-wrong
one is a footgun. Two mechanisms enforce trust here:

  1. draft -> active lifecycle with a REQUIRED owner/human confirm. Authoring
     only ever EMITS `draft` (memory_register_skill). Nothing is "trusted"
     (and nothing will surface, once surfacing ships) until memory_confirm_skill
     promotes it to `active`. A small registry of confirmed procedures beats a
     big one of plausible ones.
  2. Editing the CONTENT of an `active` skill reverts it to `draft` — the
     edited body is unconfirmed, so it must be re-confirmed. Metadata-only
     changes (pin, tags) do NOT reset status.

The VALUE of an auto-drafted skill is the FAILURE/RECOVERY trace (what broke +
how it was fixed), not the clean final command — an agent can only write that
accurately for gotchas it hit THIS session. The harvester that mines a session
trajectory lives in the agent (which has its transcript); the server's job is
to store the draft and gate the confirm. See the `gotchas`/`preconditions`
fields.

Storage: dedicated Mongo `skills` collection (source of truth for the
lifecycle/owner/version/pin state). Semantic find_skill + onboarding-bundle
injection (Phase 1) and .claude/skills materialization (Phase 2) are SURFACING,
built on top of this authoring layer in a later increment.
"""

import hashlib
import json
from typing import List

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.clients import get_mongo
from shared_memory.helpers import (
    _match_path_patterns,
    normalize_project,
    require_session,
    utc_now_iso,
)
from shared_memory.state import active_sessions

# Roles that may confirm a skill they do not own — a human operator (user) or
# an admin/owner. An agent can always confirm a skill it owns (see _can_confirm).
HUMAN_CONFIRM_ROLES = {"user", "admin", "owner"}

# Trigger text doubles as the SKILL.md frontmatter `description` and is the
# load-bearing intent-match string under Phase-2 surfacing. Keep it short.
TRIGGER_MAX_LEN = 120


def _skill_body_hash(
    name: str,
    trigger: str,
    preconditions: str,
    steps: str,
    gotchas: str,
) -> str:
    """Content-hash of the executable body (design Q9: staleness keys on
    content-hash, not re-registration events). Whitespace-normalized so a pure
    reformat does not look like a semantic change."""
    parts = [name, trigger, preconditions or "", steps, gotchas or ""]
    normalized = "\n".join(" ".join(p.split()) for p in parts)
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]


def _skill_id(project: str, name: str) -> str:
    """Stable id from (project, name). Identity is also enforced by the unique
    (project, name) Mongo index."""
    id_base = f"{project or 'shared'}:{name}"
    return f"skill_{hashlib.sha256(id_base.encode()).hexdigest()[:12]}"


def _bump_patch(version: str) -> str:
    parts = (version or "1.0.0").split(".")
    if len(parts) == 3 and parts[2].isdigit():
        parts[2] = str(int(parts[2]) + 1)
        return ".".join(parts)
    return "1.0.1"


def _can_confirm(skill_doc: dict, session_info: dict) -> bool:
    """Owner of the skill (the authoring agent) OR a human/admin/owner role."""
    caller = session_info.get("claude_instance", "")
    if caller and caller == skill_doc.get("owner"):
        return True
    return session_info.get("role", "agent") in HUMAN_CONFIRM_ROLES


def _public_skill(doc: dict) -> dict:
    """Strip Mongo's _id; return the agent-facing view."""
    out = {k: v for k, v in doc.items() if k != "_id"}
    return out


def _resolve(skills_col, session_id: str, name_or_id: str, project: str):
    """Resolve a skill by id (skill_*) or by (project, name). Returns the doc
    or None. project defaults to the caller's session project when resolving
    by name."""
    if name_or_id.startswith("skill_"):
        return skills_col.find_one({"_id": name_or_id})
    proj = normalize_project(project) if project else active_sessions[session_id].get("project")
    doc = skills_col.find_one({"project": proj or "", "name": name_or_id})
    if doc is None and proj:
        # Shared fallback (design:guideline-trim-v0): fleet-wide skills like
        # "parking" register once with project="" and resolve from any project.
        doc = skills_col.find_one({"project": "", "name": name_or_id})
    return doc


@mcp.tool()
async def memory_register_skill(
    session_id: str,
    name: str,
    trigger: str,
    steps: str,
    project: str = None,
    role: str = None,
    directory: str = None,
    preconditions: str = None,
    gotchas: str = None,
    depends_on: List[str] = None,
    tags: List[str] = None,
    version: str = None,
    ctx: Context = None,
) -> str:
    """Author (draft) a reusable, task-invoked how-to skill.

    Authoring ALWAYS produces a `draft`. Nothing is trusted until an owner or a
    human promotes it with memory_confirm_skill. Editing the body of an already-
    `active` skill reverts it to `draft` (the edit is unconfirmed). This is the
    trust gate — see the module docstring.

    Write from THIS session's failure/recovery trace, not an idealized happy
    path: the real value is in `gotchas` (what broke + how you recovered) and
    `preconditions` (the runtime gate that, unmet, makes the steps hard-fail).

    Args:
        session_id: Your session ID.
        name: Short stable skill name, e.g. "run-eval-gate". Unique per project.
        trigger: <=120 char "WHEN to use this" (SKILL.md `description`). The
            intent-match string for future surfacing — make it specific.
        steps: The procedure body (markdown). What to actually do.
        project: Project this skill belongs to (defaults to your session's).
        role: Optional role this skill is scoped to (e.g. "et-engine"). Omit
            for any role in the project.
        directory: Optional codebase path/glob this binds to (e.g. "eval/").
            Many procedures bind to a TREE regardless of role.
        preconditions: Runtime gate — what must be true to run the steps
            ("from THIS worktree, Mongo up, gold CSVs present"). Distinct from
            depends_on. An executable skill with no preconditions is a footgun.
        gotchas: Failure modes / recovery notes. Per the design, THE high-value
            field — distinct from steps.
        depends_on: ids of functions/specs/learnings this procedure relies on
            (link-tracking for later content-hash staleness).
        tags: Optional tags.
        version: Optional explicit semver; auto-increments patch on body change.
    """
    error = require_session(session_id)
    if error:
        return error

    if not name or not name.strip():
        return json.dumps({"error": "name is required"})
    if not trigger or not trigger.strip():
        return json.dumps({"error": "trigger is required (the WHEN-to-use string)"})
    if len(trigger) > TRIGGER_MAX_LEN:
        return json.dumps({
            "error": f"trigger too long ({len(trigger)} > {TRIGGER_MAX_LEN} chars)",
            "suggestion": "trigger is the SKILL.md description / intent-match string; keep it to one tight WHEN-clause.",
        })
    if not steps or not steps.strip():
        return json.dumps({"error": "steps is required (the procedure body)"})

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable — skills require persistent storage"})
    skills_col = db.skills

    session_info = active_sessions[session_id]
    now = utc_now_iso()
    # Shared-scope fix (backlog_27c0fc6bcb4f): None = default to the session's
    # project; explicit "" = SHARED skill (was silently coerced to the session
    # project by the falsy check, making shared skills unregisterable).
    if project is None:
        project = session_info.get("project")
    elif project == "":
        pass  # explicit shared scope
    else:
        project = normalize_project(project)
    owner = session_info.get("claude_instance", "unknown")

    try:
        from shared_memory.auth import require_auth
        auth_error = require_auth(session_info, "skills", project)
        if auth_error:
            return json.dumps({"error": auth_error})
    except ImportError:
        pass

    depends_on = depends_on or []
    tags = tags or []
    skill_id = _skill_id(project, name)
    body_hash = _skill_body_hash(name, trigger, preconditions, steps, gotchas)

    existing = skills_col.find_one({"_id": skill_id})
    if not existing:
        # Legacy skills created before the current _skill_id() formula carry a
        # non-matching _id (e.g. parking = skill_39c35f43227d vs formula
        # skill_6dc0c0347439). The unique (project, name) index is the real
        # identity, so fall back to it — otherwise an owner edit misses the doc,
        # falls through to insert_one, and dup-keys on (project, name). Target
        # the real _id so the update lands. (learning on skills-legacy-id.)
        legacy = skills_col.find_one({"project": project or "", "name": name})
        if legacy:
            existing = legacy
            skill_id = legacy["_id"]

    if existing:
        # Owner-only edits (mirrors specs.py). Human/admin may also edit.
        if existing.get("owner") and existing["owner"] != owner \
                and session_info.get("role", "agent") not in HUMAN_CONFIRM_ROLES:
            return json.dumps({
                "error": "Permission denied — skill owned by another agent",
                "skill": name,
                "owner": existing["owner"],
                "requester": owner,
                "suggestion": "Only the owner (or a human/admin) can edit this skill.",
            })

        content_changed = existing.get("content_hash") != body_hash
        new_version = version or (
            _bump_patch(existing.get("version", "1.0.0")) if content_changed
            else existing.get("version", "1.0.0")
        )
        # Trust property: a content edit to an active skill un-confirms it.
        if content_changed and existing.get("status") == "active":
            new_status = "draft"
            status_note = "Body changed — reverted to draft; re-confirm to re-activate."
        else:
            new_status = existing.get("status", "draft")
            status_note = None

        update = {
            "name": name,
            "trigger": trigger,
            "steps": steps,
            "project": project or "",
            "scope": {"project": project or "", "role": role, "directory": directory},
            "preconditions": preconditions,
            "gotchas": gotchas,
            "depends_on": depends_on,
            "tags": tags,
            "version": new_version,
            "content_hash": body_hash,
            "status": new_status,
            "updated": now,
            "updated_by": owner,
        }
        if content_changed:
            # A new unconfirmed body invalidates the prior confirmation.
            update["confirmed_by"] = None
            update["confirmed_at"] = None
        skills_col.update_one({"_id": skill_id}, {"$set": update})
        action = "updated"
        result_status = new_status
        result_version = new_version
    else:
        doc = {
            "_id": skill_id,
            "skill_id": skill_id,
            "name": name,
            "trigger": trigger,
            "steps": steps,
            "project": project or "",
            "scope": {"project": project or "", "role": role, "directory": directory},
            "preconditions": preconditions,
            "gotchas": gotchas,
            "depends_on": depends_on,
            "tags": tags,
            "version": version or "1.0.0",
            "content_hash": body_hash,
            "status": "draft",
            "pin": False,
            "owner": owner,
            "created": now,
            "updated": now,
            "created_by": owner,
            "updated_by": owner,
            "confirmed_by": None,
            "confirmed_at": None,
        }
        skills_col.insert_one(doc)
        action = "registered"
        result_status = "draft"
        result_version = doc["version"]
        status_note = None

    try:
        from shared_memory.audit import log_audit
        log_audit(f"skill.{action}", owner, project or "",
                  {"skill": name, "status": result_status}, session_id)
    except Exception:
        pass

    result = {
        "status": action,
        "id": skill_id,
        "name": name,
        "skill_status": result_status,
        "version": result_version,
        "note": "Draft only — promote with memory_confirm_skill before it is trusted/surfaced.",
    }
    if status_note:
        result["note"] = status_note + " " + result["note"]
    return json.dumps(result, indent=2)


@mcp.tool()
async def memory_confirm_skill(
    session_id: str,
    name_or_id: str,
    project: str = None,
    ctx: Context = None,
) -> str:
    """Promote a draft skill to `active` (the trust gate).

    Allowed for the skill's OWNER (the authoring agent confirming its own work
    after review) or a human/admin/owner role. Nothing surfaces a skill until
    it is active. Confirming an already-active skill is a no-op.

    Args:
        session_id: Your session ID.
        name_or_id: skill id (skill_*) or the skill name.
        project: Project (defaults to your session's) when resolving by name.
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})
    skills_col = db.skills
    session_info = active_sessions[session_id]

    doc = _resolve(skills_col, session_id, name_or_id, project)
    if not doc:
        return json.dumps({"error": f"Skill '{name_or_id}' not found"})

    if not _can_confirm(doc, session_info):
        return json.dumps({
            "error": "Permission denied — only the skill owner or a human/admin can confirm",
            "skill": doc.get("name"),
            "owner": doc.get("owner"),
            "requester": session_info.get("claude_instance"),
        })

    if doc.get("status") == "active":
        return json.dumps({
            "status": "already_active",
            "id": doc["_id"],
            "name": doc.get("name"),
            "confirmed_by": doc.get("confirmed_by"),
            "confirmed_at": doc.get("confirmed_at"),
        })

    now = utc_now_iso()
    confirmer = session_info.get("claude_instance", "unknown")
    skills_col.update_one(
        {"_id": doc["_id"]},
        {"$set": {"status": "active", "confirmed_by": confirmer,
                  "confirmed_at": now, "updated": now}},
    )

    try:
        from shared_memory.audit import log_audit
        log_audit("skill.confirmed", confirmer, doc.get("scope", {}).get("project", ""),
                  {"skill": doc.get("name")}, session_id)
    except Exception:
        pass

    return json.dumps({
        "status": "confirmed",
        "id": doc["_id"],
        "name": doc.get("name"),
        "skill_status": "active",
        "confirmed_by": confirmer,
        "confirmed_at": now,
    }, indent=2)


@mcp.tool()
async def memory_get_skill(
    session_id: str,
    name_or_id: str,
    project: str = None,
    ctx: Context = None,
) -> str:
    """Fetch one skill (full body) by id (skill_*) or by name.

    Args:
        session_id: Your session ID.
        name_or_id: skill id (skill_*) or the skill name.
        project: Project (defaults to your session's) when resolving by name.
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})
    skills_col = db.skills

    doc = _resolve(skills_col, session_id, name_or_id, project)
    if not doc:
        return json.dumps({"error": f"Skill '{name_or_id}' not found"})
    return json.dumps(_public_skill(doc), indent=2)


@mcp.tool()
async def memory_list_skills(
    session_id: str,
    project: str = None,
    status: str = None,
    role: str = None,
    directory: str = None,
    include_drafts: bool = True,
    limit: int = 50,
    ctx: Context = None,
) -> str:
    """List skills, scoped by project and optional role/directory/status.

    Returns headers (no full steps body) — use memory_get_skill for the body.
    Ranking: pinned first, then active-before-draft, then most-recently-updated
    (procedures are evergreen, so NOT recency-primary — pin and confirmed-status
    lead, per design hygiene).

    Args:
        session_id: Your session ID.
        project: Project (defaults to your session's).
        status: Filter to "draft" or "active".
        role: Filter to skills scoped to this role (also returns role-agnostic).
        directory: Filter to skills bound to this directory (also returns
            directory-agnostic ones).
        include_drafts: Include drafts (default True). Ignored if `status` set.
        limit: Max skills (default 50).
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})
    skills_col = db.skills

    proj = normalize_project(project) if project else active_sessions[session_id].get("project")
    query = {"project": proj or ""}
    if status:
        query["status"] = status
    elif not include_drafts:
        query["status"] = "active"

    docs = list(skills_col.find(query).limit(max(1, min(limit, 200))))

    # role / directory scope filter: a match means the skill is unscoped on that
    # axis (applies to everyone) OR scoped to exactly the requested value.
    def scope_ok(doc):
        sc = doc.get("scope", {}) or {}
        if role is not None and sc.get("role") not in (None, "", role):
            return False
        if directory is not None and sc.get("directory") not in (None, "", directory):
            return False
        return True

    docs = [d for d in docs if scope_ok(d)]

    # Stable two-pass sort: updated-desc first (the tiebreaker), then the
    # primary keys pin>active-before-draft. Python's sort is stable, so the
    # updated-desc order survives within each pin/status tier.
    status_rank = {"active": 0, "draft": 1}
    docs.sort(key=lambda d: d.get("updated", ""), reverse=True)
    docs.sort(key=lambda d: (
        0 if d.get("pin") else 1,
        status_rank.get(d.get("status"), 2),
    ))

    items = [{
        "id": d["_id"],
        "name": d.get("name"),
        "trigger": d.get("trigger"),
        "status": d.get("status"),
        "version": d.get("version"),
        "pin": d.get("pin", False),
        "scope": d.get("scope", {}),
        "owner": d.get("owner"),
        "updated": d.get("updated"),
    } for d in docs]

    return json.dumps({"count": len(items), "project": proj, "skills": items}, indent=2)


@mcp.tool()
async def memory_pin_skill(
    session_id: str,
    name_or_id: str,
    pin: bool = True,
    project: str = None,
    ctx: Context = None,
) -> str:
    """Pin/unpin a skill so it ranks first in listings/surfacing.

    Owner or human/admin only (same gate as confirm). Pinning is metadata-only —
    it does NOT change draft/active status.

    Args:
        session_id: Your session ID.
        name_or_id: skill id (skill_*) or name.
        pin: True to pin, False to unpin.
        project: Project (defaults to your session's) when resolving by name.
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})
    skills_col = db.skills
    session_info = active_sessions[session_id]

    doc = _resolve(skills_col, session_id, name_or_id, project)
    if not doc:
        return json.dumps({"error": f"Skill '{name_or_id}' not found"})
    if not _can_confirm(doc, session_info):
        return json.dumps({
            "error": "Permission denied — only the skill owner or a human/admin can pin",
            "skill": doc.get("name"),
        })

    skills_col.update_one({"_id": doc["_id"]},
                          {"$set": {"pin": bool(pin), "updated": utc_now_iso()}})
    return json.dumps({"status": "pinned" if pin else "unpinned",
                       "id": doc["_id"], "name": doc.get("name")}, indent=2)


# ── Phase-1 surfacing: onboarding-bundle injection ──────────────────────────
# NOT an @mcp.tool — a plain helper called by memory_start_session to put
# scope-matched ACTIVE skills into the onboarding bundle. This is the cheap,
# de-risking half of surfacing (design Q7 Phase-1-first): the SHORT go->do->park
# session sees its relevant procedures at the moment session-start IS the moment
# of doing. Phase 2 (.claude/skills materialization for mid-turn match) is later.
def get_scope_matched_skills(
    project: str,
    claude_instance: str = None,
    role_description: str = None,
    working_directory: str = None,
    limit: int = 8,
):
    """Return up to `limit` ACTIVE skills scoped to this agent, as bundle
    headers [{id, name, trigger}]. Drafts NEVER surface (trust gate). Best-
    effort: any failure returns [] so it can't break session start.

    Scope match (each axis is permissive — an unset axis on the skill applies
    to everyone):
      - project : exact (required).
      - directory: skill.scope.directory unset → always; else included only
        when working_directory is known AND matches (helpers._match_path_patterns).
        Unknown working_directory is permissive (included) — we can't disprove
        relevance, and hiding a procedure is worse than showing one extra.
      - role: skill.scope.role unset → always; else included when it equals the
        agent's instance name OR appears in its role_description.

    Ranking: pinned first, then most-recently-updated. Headers only — the agent
    pulls the full body with memory_get_skill when it actually needs it.
    """
    docs = _active_scope_matched_docs(
        project, claude_instance, role_description, working_directory, limit
    )
    return [{"id": d["_id"], "name": d.get("name"), "trigger": d.get("trigger")}
            for d in docs]


def _active_scope_matched_docs(
    project: str,
    claude_instance: str = None,
    role_description: str = None,
    working_directory: str = None,
    limit: int = 8,
    strict: bool = False,
):
    """Shared scope-matcher: ACTIVE skills in `project` matching this agent's
    role/directory, pinned-first then updated-desc, capped at `limit`. Returns
    full Mongo docs. Used by both the Phase-1 header surfacer and the Phase-2
    SKILL.md exporter so they never diverge.

    strict controls failure mode. Default (strict=False) is BEST-EFFORT: any
    DB-unavailability or query error returns [] so it can never break session
    start. strict=True is FAIL-LOUD: an unreachable DB (or a query error)
    RAISES instead of masquerading as an empty result. The export path uses
    strict=True because its consumer (the launcher) PRUNES SKILL.md files by
    footer — a silent empty result on a DB outage would make it delete every
    materialized skill. See build_skill_export."""
    try:
        db = get_mongo()
        if db is None or not project:
            if strict and db is None:
                raise RuntimeError("MongoDB unavailable")
            return []
        proj = normalize_project(project)
        docs = list(db.skills.find({"project": proj, "status": "active"}))

        rd = (role_description or "").lower()

        def matches(doc):
            sc = doc.get("scope", {}) or {}
            directory = sc.get("directory")
            if directory:
                if working_directory and not _match_path_patterns(working_directory, [directory]):
                    return False
            srole = sc.get("role")
            if srole:
                if srole == claude_instance:
                    return True
                if rd and srole.lower() in rd:
                    return True
                return False
            return True

        docs = [d for d in docs if matches(d)]
        docs.sort(key=lambda d: d.get("updated", ""), reverse=True)
        docs.sort(key=lambda d: 0 if d.get("pin") else 1)
        return docs[:max(1, limit)]
    except Exception:
        if strict:
            raise
        return []


# ── Phase-2 surfacing: SKILL.md materialization payload ─────────────────────
# The server canNOT write .claude/skills/ on each agent's box (it's a container
# on sage; those dirs live on the inbox/nimbus/… hosts). So Phase 2 is a TWO-
# sided contract: the server EXPOSES ready-to-write SKILL.md docs here, and the
# per-box LAUNCHER (junto-launch.sh / junto-workspace.ps1, tlemmons/junto) writes
# them under .claude/skills/<name>/SKILL.md BEFORE Claude Code boots (CC discovers
# skills at startup; a mid-session write is too late). Contract: spec
# interface:skill-materialization-v0.
def _skillmd_frontmatter_value(s: str) -> str:
    """YAML-safe single-line value for the SKILL.md frontmatter. CC reads
    `name`/`description` from here; keep them flat (quote, strip newlines)."""
    flat = " ".join((s or "").split())
    return '"' + flat.replace('"', "'") + '"'


def render_skill_md(doc: dict) -> str:
    """Render a skill doc to Claude-Code SKILL.md text: YAML frontmatter
    (name + description=trigger, the fields CC's matcher reads) followed by a
    markdown body (when-to-use / preconditions / steps / gotchas)."""
    name = doc.get("name", "unnamed")
    trigger = doc.get("trigger", "")
    fm = [
        "---",
        f"name: {_skillmd_frontmatter_value(name)}",
        f"description: {_skillmd_frontmatter_value(trigger)}",
        "---",
        "",
        f"# {name}",
        "",
        f"**When to use:** {trigger}",
    ]
    if doc.get("preconditions"):
        fm += ["", "## Preconditions", "", doc["preconditions"]]
    fm += ["", "## Steps", "", doc.get("steps", "")]
    if doc.get("gotchas"):
        fm += ["", "## Gotchas", "", doc["gotchas"]]
    deps = doc.get("depends_on") or []
    if deps:
        fm += ["", "## Depends on", "", ", ".join(deps)]
    fm += [
        "",
        "---",
        f"<!-- junto skill {doc.get('_id')} v{doc.get('version')} "
        f"status={doc.get('status')} — materialized from junto-memory; "
        f"edit the source via memory_register_skill, not this file. -->",
        "",
    ]
    return "\n".join(fm)


def build_skill_export(
    project: str,
    role: str = None,
    role_description: str = None,
    working_directory: str = None,
    limit: int = 25,
) -> dict:
    """Shared export CORE for the Phase-2 SKILL.md producer. Called by BOTH the
    memory_export_skills MCP tool AND the POST /export-skills REST route so the
    two surfaces can never diverge in shape (the REST contract promises a body
    IDENTICAL to memory_export_skills).

    Session-less: takes the scope axes explicitly — no active_sessions lookup —
    which is exactly why the REST route (no session to default from) can reuse
    it. `project` is normalized here (idempotent). Fail-loud: passes strict=True
    to the matcher so a DB outage RAISES rather than returning a misleading
    empty export (the launcher prunes by footer; count:0 on an outage would
    wipe every materialized skill). Drafts are never exported.

    Returns the exact wire shape:
        {project, count, skills: [{id, name, relpath, content}]}
    """
    proj = normalize_project(project)
    docs = _active_scope_matched_docs(
        proj, role, role_description, working_directory, limit, strict=True
    )
    skills = [{
        "id": d["_id"],
        "name": d.get("name"),
        "relpath": f"{d.get('name')}/SKILL.md",
        "content": render_skill_md(d),
    } for d in docs]
    return {"project": proj, "count": len(skills), "skills": skills}


@mcp.tool()
async def memory_export_skills(
    session_id: str,
    project: str = None,
    role: str = None,
    directory: str = None,
    working_directory: str = None,
    limit: int = 25,
    ctx: Context = None,
) -> str:
    """Phase-2 producer: export ACTIVE scope-matched skills as ready-to-write
    SKILL.md materialization payloads. The per-box LAUNCHER consumes this and
    writes each `relpath` under .claude/skills/ before Claude Code starts, so
    CC's native matcher fires the skill MID-TURN. See interface:skill-
    materialization-v0. The server cannot write these files itself.

    Args:
        session_id: Your session ID.
        project: Project to export (defaults to your session's).
        role: Role to scope to (defaults to your session's agent name). Role-
            agnostic skills are always included.
        directory / working_directory: Directory scope to match (working_directory
            is the agent's cwd; directory-agnostic skills always included).
        limit: Max skills to export (default 25).

    Returns: {project, count, skills: [{id, name, relpath, content}]} where
    relpath is "<name>/SKILL.md" and content is the full SKILL.md text.
    """
    error = require_session(session_id)
    if error:
        return error

    session_info = active_sessions[session_id]
    proj = project if project else session_info.get("project")
    role = role if role is not None else session_info.get("claude_instance")

    try:
        result = build_skill_export(
            proj, role, session_info.get("role_description"), working_directory, limit
        )
    except Exception as e:
        # Fail loud: don't hand the pruning launcher a misleading empty export.
        return json.dumps({"error": f"skill export failed: {e}"})
    return json.dumps(result, indent=2)
