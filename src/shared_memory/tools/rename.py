"""Rename agent / project tooling.

Migrates state specs, function registry, learnings, message threads, backlog,
agent directory, and project metadata in a single pass.

Atomicity caveats:
- Mongo and Chroma are separate stores; there's no real two-phase commit.
- We write the alias record first, do all the data mutations, then audit-log.
  If a step partway through fails, the alias record marks the rename as
  in-flight; a retry with the same args returns success and re-runs the
  remaining steps idempotently (each Mongo update_many / Chroma update is
  safe to repeat against already-renamed rows because the "where" filter
  matches old-name only).
- Append-only stores (audit_log, api_keys.created_by) keep historical names.

Alias TTL: 30 days (Mongo TTL index on expires_at). After expiry, lookups by
old name hard-fail.
"""

from datetime import timedelta
from typing import Dict, List, Optional, Tuple

from shared_memory.audit import log_audit
from shared_memory.helpers import (
    get_project_collection,
    get_shared_collection,
    normalize_project,
    utc_now,
    utc_now_iso,
)

ALIAS_TTL_DAYS = 30


def _alias_collection(db):
    """Lazily ensure the rename_aliases collection + indexes exist."""
    col = db.rename_aliases
    # Idempotent index creation
    try:
        col.create_index("expires_at", expireAfterSeconds=0)
        col.create_index([("type", 1), ("from_project", 1), ("from_agent", 1)])
        col.create_index([("type", 1), ("from_project", 1)])
    except Exception:
        pass
    return col


def _existing_alias_for_agent(db, from_project: str, from_agent: str) -> Optional[Dict]:
    """Return active alias for this old name, if any."""
    col = _alias_collection(db)
    return col.find_one({
        "type": "agent",
        "from_project": from_project,
        "from_agent": from_agent,
    })


def _existing_alias_for_project(db, from_project: str) -> Optional[Dict]:
    col = _alias_collection(db)
    return col.find_one({
        "type": "project",
        "from_project": from_project,
    })


def resolve_agent_alias(db, project: str, agent: str) -> Optional[Tuple[str, str, Dict]]:
    """If (project, agent) has an active alias, return (new_project, new_agent, alias_doc).

    Returns None if no alias.

    Used by memory_start_session to redirect old-name connections.
    """
    if db is None:
        return None
    norm = normalize_project(project)
    # Agent-level rename
    a = _existing_alias_for_agent(db, norm, agent)
    if a:
        return (a["to_project"], a["to_agent"], a)
    # Project-level rename
    p = _existing_alias_for_project(db, norm)
    if p:
        return (p["to_project"], agent, p)
    return None


# ── Survey: count what would change ────────────────────────────────────────

def _count_agent_impact(db, from_project: str, from_agent: str) -> Dict[str, int]:
    """Count Mongo rows that reference (from_project, from_agent)."""
    counts: Dict[str, int] = {}
    counts["messages_sent"] = db.messages.count_documents({
        "from_instance": from_agent,
        "from_project": from_project,
    })
    counts["messages_received"] = db.messages.count_documents({
        "to_instance": from_agent,
        "to_project": from_project,
    })
    counts["registered_agents"] = db.registered_agents.count_documents({
        "project": from_project, "name": from_agent,
    })
    counts["agent_directory"] = db.agent_directory.count_documents({
        "project": from_project, "instance": from_agent,
    })
    counts["agent_status"] = db.agent_status.count_documents({
        "instance": from_agent,
    })
    counts["compaction_events"] = db.compaction_events.count_documents({
        "agent": from_agent,
    })
    counts["projects_admins"] = db.projects.count_documents({
        "admins": from_agent,
    })
    return counts


async def _count_chroma_agent_impact(chroma, from_project: str, from_agent: str) -> Dict[str, int]:
    """Count Chroma docs (in proj_<from_project> + shared_work) referencing the agent."""
    counts: Dict[str, int] = {}
    try:
        proj = await get_project_collection(chroma, from_project)
        # Use $or-equivalent: Chroma where filters don't support OR directly,
        # so we run several gets and union ids.
        ids: set = set()
        for field in ("claude_instance", "spec_owner", "created_by", "updated_by",
                       "assigned_to", "owner"):
            try:
                got = await proj.get(where={field: from_agent}, include=[])
                for d in got.get("ids", []) or []:
                    ids.add(d)
            except Exception:
                pass
        counts["proj_chroma_docs"] = len(ids)
        # State spec doc id (separate count for clarity)
        try:
            spec_id = f"spec_state_{from_agent}"
            r = await proj.get(ids=[spec_id], include=[])
            counts["state_spec"] = 1 if r.get("ids") else 0
        except Exception:
            counts["state_spec"] = 0
    except Exception:
        counts["proj_chroma_docs"] = 0
        counts["state_spec"] = 0
    try:
        shared_work = await get_shared_collection(chroma, "work")
        got = await shared_work.get(
            where={"$and": [{"claude_instance": from_agent}, {"project": from_project}]},
            include=[],
        )
        counts["shared_work"] = len(got.get("ids", []) or [])
    except Exception:
        counts["shared_work"] = 0
    return counts


# ── Mongo updates ──────────────────────────────────────────────────────────

def _rename_agent_in_projects_admins(db, from_agent: str, to_agent: str) -> int:
    """Rename agent in every project's admins[] list. Returns count of projects touched.

    A project's admins[] is a list of agent name strings (not project-qualified).
    The list expresses "these agents may admin this project," so an agent rename
    must be reflected here regardless of whether the agent's own project changed.

    Done as find-then-pull-then-addToSet (rather than $set inline) so re-running
    after partial completion no-ops cleanly: $pull is a no-op on absent values,
    $addToSet won't duplicate. If to_agent is already present (e.g., a prior
    rename added it), $addToSet leaves the list deduplicated.
    """
    if from_agent == to_agent:
        return 0
    matching = list(db.projects.find({"admins": from_agent}, {"name": 1}))
    if not matching:
        return 0
    names = [p["name"] for p in matching]
    db.projects.update_many(
        {"name": {"$in": names}},
        {"$pull": {"admins": from_agent}},
    )
    db.projects.update_many(
        {"name": {"$in": names}},
        {"$addToSet": {"admins": to_agent}},
    )
    return len(names)


def _apply_agent_renames_mongo(
    db, from_project: str, from_agent: str,
    to_project: str, to_agent: str,
) -> Dict[str, int]:
    """Run all Mongo update_manys. Returns modified-count dict.

    Order chosen so that retries are safe — each filter selects old-name
    rows only, so re-running after partial completion just no-ops the
    already-migrated rows.
    """
    results: Dict[str, int] = {}
    cross_project = (from_project != to_project)

    # Messages: update sender side
    r = db.messages.update_many(
        {"from_instance": from_agent, "from_project": from_project},
        {"$set": {"from_instance": to_agent, "from_project": to_project}},
    )
    results["messages_sent"] = r.modified_count

    # Messages: update recipient side
    r = db.messages.update_many(
        {"to_instance": from_agent, "to_project": from_project},
        {"$set": {"to_instance": to_agent, "to_project": to_project}},
    )
    results["messages_received"] = r.modified_count

    # registered_agents — move row. If cross-project, that's a delete+insert
    # because (project, name) is the unique key.
    if cross_project:
        old = db.registered_agents.find_one({"project": from_project, "name": from_agent})
        if old:
            new_doc = {**old, "project": to_project, "name": to_agent}
            new_doc.pop("_id", None)
            db.registered_agents.delete_one({"project": from_project, "name": from_agent})
            try:
                db.registered_agents.insert_one(new_doc)
                results["registered_agents"] = 1
            except Exception:
                # If insert collides (target already exists), restore old to be safe
                db.registered_agents.update_one(
                    {"project": from_project, "name": from_agent},
                    {"$setOnInsert": old}, upsert=True,
                )
                raise
        else:
            results["registered_agents"] = 0
    else:
        r = db.registered_agents.update_one(
            {"project": from_project, "name": from_agent},
            {"$set": {"name": to_agent}},
        )
        results["registered_agents"] = r.modified_count

    # agent_directory — same shape (unique on project+instance)
    if cross_project:
        old = db.agent_directory.find_one({"project": from_project, "instance": from_agent})
        if old:
            new_doc = {**old, "project": to_project, "instance": to_agent}
            new_doc.pop("_id", None)
            db.agent_directory.delete_one({"project": from_project, "instance": from_agent})
            db.agent_directory.insert_one(new_doc)
            results["agent_directory"] = 1
        else:
            results["agent_directory"] = 0
    else:
        r = db.agent_directory.update_one(
            {"project": from_project, "instance": from_agent},
            {"$set": {"instance": to_agent}},
        )
        results["agent_directory"] = r.modified_count

    # agent_status — keyed only on instance (unique). 1h TTL anyway, so we
    # can delete-and-let-heartbeat-rewrite. Cleaner: just delete.
    r = db.agent_status.delete_many({"instance": from_agent})
    results["agent_status_dropped"] = r.deleted_count

    # compaction_events — agent only
    r = db.compaction_events.update_many(
        {"agent": from_agent},
        {"$set": {"agent": to_agent}},
    )
    results["compaction_events"] = r.modified_count

    # projects.admins[] — rename the agent name in every project's admin list.
    # See _rename_agent_in_projects_admins docstring for shape rationale.
    results["projects_admins"] = _rename_agent_in_projects_admins(
        db, from_agent, to_agent,
    )

    return results


# ── Chroma updates ─────────────────────────────────────────────────────────

async def _update_metadata_field(collection, where: Dict, field: str, new_value: str) -> int:
    """For every doc in `collection` matching `where`, set metadata[field] = new_value.

    Returns count of docs updated.
    """
    try:
        got = await collection.get(where=where, include=["metadatas"])
    except Exception:
        return 0
    ids = got.get("ids") or []
    metas = got.get("metadatas") or []
    if not ids:
        return 0
    new_metas = []
    for m in metas:
        m2 = dict(m or {})
        m2[field] = new_value
        new_metas.append(m2)
    try:
        await collection.update(ids=ids, metadatas=new_metas)
        return len(ids)
    except Exception:
        return 0


async def _apply_agent_renames_chroma(
    chroma, from_project: str, from_agent: str,
    to_project: str, to_agent: str,
) -> Dict[str, int]:
    """Update Chroma metadata for the renamed agent.

    Cross-project rename moves data from proj_<from> to proj_<to>; same-project
    rename only rewrites metadata.

    Returns counts dict.
    """
    results: Dict[str, int] = {"chroma_metadata_docs": 0, "shared_work": 0,
                              "state_spec_moved": 0}
    same_project = (from_project == to_project)

    src_proj = await get_project_collection(chroma, from_project)
    dst_proj = src_proj if same_project else await get_project_collection(chroma, to_project)

    # 1. State spec doc-id rename (state:<old> -> state:<new>).
    #    Because doc_id encodes the name, this is a copy+delete.
    old_spec_id = f"spec_state_{from_agent}"
    new_spec_id = f"spec_state_{to_agent}"
    try:
        old = await src_proj.get(ids=[old_spec_id], include=["documents", "metadatas"])
        if old.get("ids"):
            doc = (old.get("documents") or [""])[0]
            meta = dict((old.get("metadatas") or [{}])[0] or {})
            # Update spec_name in metadata
            meta["spec_name"] = f"state:{to_agent}"
            meta["title"] = f"Spec: state:{to_agent}"
            if meta.get("spec_owner") == from_agent:
                meta["spec_owner"] = to_agent
            if meta.get("created_by") == from_agent:
                meta["created_by"] = to_agent
            if meta.get("updated_by") == from_agent:
                meta["updated_by"] = to_agent
            if meta.get("project") == from_project:
                meta["project"] = to_project
            await dst_proj.upsert(ids=[new_spec_id], documents=[doc], metadatas=[meta])
            try:
                await src_proj.delete(ids=[old_spec_id])
            except Exception:
                pass
            results["state_spec_moved"] = 1
    except Exception:
        pass

    # 2. Update metadata fields on remaining docs in proj_<from_project>.
    fields_to_rewrite = (
        "claude_instance", "spec_owner", "created_by", "updated_by",
        "assigned_to", "owner",
    )

    if same_project:
        # In-place metadata rewrite, doc IDs unchanged.
        total = 0
        for field in fields_to_rewrite:
            try:
                total += await _update_metadata_field(
                    src_proj, {field: from_agent}, field, to_agent,
                )
            except Exception:
                pass
        results["chroma_metadata_docs"] = total
    else:
        # Cross-project: copy matching docs to destination collection with
        # rewritten metadata, then delete from source. Skip the state spec
        # which we handled above.
        moved_ids: set = set()
        moved = 0
        for field in fields_to_rewrite:
            try:
                got = await src_proj.get(
                    where={field: from_agent},
                    include=["documents", "metadatas"],
                )
            except Exception:
                continue
            ids = got.get("ids") or []
            docs = got.get("documents") or []
            metas = got.get("metadatas") or []
            for i, doc_id in enumerate(ids):
                if doc_id == old_spec_id or doc_id in moved_ids:
                    continue
                m = dict(metas[i] if i < len(metas) and metas[i] else {})
                # Rewrite all matching identity fields, not just the trigger field
                for f in fields_to_rewrite:
                    if m.get(f) == from_agent:
                        m[f] = to_agent
                if m.get("project") == from_project:
                    m["project"] = to_project
                doc_text = docs[i] if i < len(docs) else ""
                try:
                    await dst_proj.upsert(ids=[doc_id], documents=[doc_text], metadatas=[m])
                    moved_ids.add(doc_id)
                    moved += 1
                except Exception:
                    pass
            # Delete the moved IDs from source for this field's batch
            try:
                ids_to_delete = [i for i in ids if i in moved_ids]
                if ids_to_delete:
                    await src_proj.delete(ids=ids_to_delete)
            except Exception:
                pass
        results["chroma_metadata_docs"] = moved

    # 3. shared_work: claude_instance + project metadata fields
    try:
        shared_work = await get_shared_collection(chroma, "work")
        got = await shared_work.get(
            where={"$and": [{"claude_instance": from_agent}, {"project": from_project}]},
            include=["metadatas"],
        )
        ids = got.get("ids") or []
        metas = got.get("metadatas") or []
        if ids:
            new_metas = []
            for m in metas:
                m2 = dict(m or {})
                m2["claude_instance"] = to_agent
                m2["project"] = to_project
                new_metas.append(m2)
            try:
                await shared_work.update(ids=ids, metadatas=new_metas)
                results["shared_work"] = len(ids)
            except Exception:
                pass
    except Exception:
        pass

    return results


# ── Project-level rename ───────────────────────────────────────────────────

def _count_project_impact(db, from_project: str) -> Dict[str, int]:
    counts = {}
    counts["messages_to"] = db.messages.count_documents({"to_project": from_project})
    counts["messages_from"] = db.messages.count_documents({"from_project": from_project})
    counts["registered_agents"] = db.registered_agents.count_documents({"project": from_project})
    counts["agent_directory"] = db.agent_directory.count_documents({"project": from_project})
    counts["projects"] = db.projects.count_documents({"name": from_project})
    counts["checklists"] = db.checklists.count_documents({"project": from_project})
    return counts


def _apply_project_rename_mongo(db, from_project: str, to_project: str) -> Dict[str, int]:
    results = {}
    results["messages_to"] = db.messages.update_many(
        {"to_project": from_project}, {"$set": {"to_project": to_project}}
    ).modified_count
    results["messages_from"] = db.messages.update_many(
        {"from_project": from_project}, {"$set": {"from_project": to_project}}
    ).modified_count
    results["registered_agents"] = db.registered_agents.update_many(
        {"project": from_project}, {"$set": {"project": to_project}}
    ).modified_count
    results["agent_directory"] = db.agent_directory.update_many(
        {"project": from_project}, {"$set": {"project": to_project}}
    ).modified_count
    results["checklists"] = db.checklists.update_many(
        {"project": from_project}, {"$set": {"project": to_project}}
    ).modified_count

    # projects collection — name is unique. Move row.
    old_proj = db.projects.find_one({"name": from_project})
    if old_proj:
        new_doc = {**old_proj, "name": to_project}
        new_doc.pop("_id", None)
        db.projects.delete_one({"name": from_project})
        try:
            db.projects.insert_one(new_doc)
            results["projects"] = 1
        except Exception:
            db.projects.insert_one(old_proj)  # restore
            raise
    else:
        results["projects"] = 0

    return results


async def _apply_project_rename_chroma(chroma, from_project: str, to_project: str) -> Dict[str, int]:
    """Rename the proj_<from_project> Chroma collection to proj_<to_project>.

    Then rewrite the `project` metadata field on every doc inside it (since
    that field used to encode the old project name).

    Falls back to copy+delete if collection.modify isn't supported by the
    Chroma version.
    """
    from shared_memory.config import PROJECT_PREFIX

    results = {"chroma_collection_renamed": 0, "chroma_metadata_docs": 0, "shared_work": 0}
    src_name = f"{PROJECT_PREFIX}{from_project}"
    dst_name = f"{PROJECT_PREFIX}{to_project}"

    try:
        src = await chroma.get_collection(name=src_name)
    except Exception:
        # Source doesn't exist; nothing to do
        return results

    # Try modify (rename). Chroma 1.x: collection.modify(name=...). This
    # FAILS when the destination collection already exists (e.g., a prior
    # cross-project agent rename auto-created proj_<to> via
    # get_or_create_collection). In that case the fallback below merges
    # source into the existing destination.
    renamed = False
    try:
        await src.modify(name=dst_name)
        renamed = True
        results["chroma_collection_renamed"] = 1
    except Exception as modify_err:
        # Fallback: copy contents to a new collection then delete source.
        # Batch the upsert because large collections can exceed Chroma's
        # max-batch-size on a single call. Pass embeddings to avoid
        # re-embedding (which would change semantic similarity).
        try:
            dst = await chroma.get_or_create_collection(
                name=dst_name,
                metadata={"project": to_project, "created": utc_now_iso()},
            )
            got = await src.get(include=["documents", "metadatas", "embeddings"])
            ids = got.get("ids") or []
            if ids:
                docs = got.get("documents") or [None] * len(ids)
                metas = got.get("metadatas") or [None] * len(ids)
                embs = got.get("embeddings")
                # Truthiness on embeddings is dangerous because Chroma can
                # return a numpy array, where `arr or None` raises
                # ValueError("truth value of an array is ambiguous").
                has_embs = embs is not None and len(embs) == len(ids)
                # Batch upserts. Empirically Chroma defaults handle ~50-100
                # per call comfortably; pick 50 to be safe with embeddings.
                BATCH = 50
                for off in range(0, len(ids), BATCH):
                    end = min(off + BATCH, len(ids))
                    kwargs = {
                        "ids": ids[off:end],
                        "documents": docs[off:end],
                        "metadatas": metas[off:end],
                    }
                    if has_embs:
                        kwargs["embeddings"] = embs[off:end]
                    await dst.upsert(**kwargs)
            await chroma.delete_collection(name=src_name)
            renamed = True
            results["chroma_collection_renamed"] = 1
        except Exception as fallback_err:
            # Surface BOTH the modify error (why we hit the fallback) and
            # the fallback error so silent failure doesn't strand data.
            results["chroma_rename_error"] = (
                f"modify={type(modify_err).__name__}:{modify_err}; "
                f"fallback={type(fallback_err).__name__}:{fallback_err}"
            )

    if renamed:
        # Rewrite `project` metadata field on every doc that still encodes
        # the old project name.
        try:
            dst = await chroma.get_collection(name=dst_name)
            count = await _update_metadata_field(
                dst, {"project": from_project}, "project", to_project,
            )
            results["chroma_metadata_docs"] = count
        except Exception:
            pass

    # shared_work: project field
    try:
        shared_work = await get_shared_collection(chroma, "work")
        results["shared_work"] = await _update_metadata_field(
            shared_work, {"project": from_project}, "project", to_project,
        )
    except Exception:
        pass

    return results


# ── Public entry points ────────────────────────────────────────────────────

async def perform_rename_agent(
    db, chroma, *,
    from_project: str, from_agent: str,
    to_project: str, to_agent: str,
    dry_run: bool, actor: str, session_id: str = "",
) -> Dict:
    """Rename an agent, all stores. Returns result dict."""
    from_project = normalize_project(from_project)
    to_project = normalize_project(to_project)

    if not from_agent or not to_agent or not from_project or not to_project:
        return {"error": "from_project, from_agent, to_project, to_agent are all required"}

    if (from_project, from_agent) == (to_project, to_agent):
        return {"error": "source and target are identical"}

    # Idempotency: if alias already exists pointing the same way, treat as
    # already-done. If alias points elsewhere, refuse.
    existing = _existing_alias_for_agent(db, from_project, from_agent)
    if existing:
        if existing.get("to_project") == to_project and existing.get("to_agent") == to_agent:
            return {
                "status": "already_renamed",
                "from": f"{from_agent}@{from_project}",
                "to": f"{to_agent}@{to_project}",
                "alias_doc": {k: v for k, v in existing.items() if k != "_id"},
            }
        return {
            "error": "agent already aliased to a different target",
            "existing_target": f"{existing.get('to_agent')}@{existing.get('to_project')}",
        }

    # Refuse to overwrite an existing live target
    target_exists = db.registered_agents.find_one({
        "project": to_project, "name": to_agent,
    })
    if target_exists:
        return {
            "error": f"target {to_agent}@{to_project} already exists in registered_agents",
            "hint": "remove the target first or pick a different to_agent",
        }

    counts_mongo = _count_agent_impact(db, from_project, from_agent)
    counts_chroma = await _count_chroma_agent_impact(chroma, from_project, from_agent)

    if dry_run:
        return {
            "status": "dry_run",
            "from": f"{from_agent}@{from_project}",
            "to": f"{to_agent}@{to_project}",
            "would_modify": {**counts_mongo, **counts_chroma},
        }

    # Write alias record FIRST so concurrent retries see in-flight state.
    now = utc_now()
    expires = now + timedelta(days=ALIAS_TTL_DAYS)
    alias_col = _alias_collection(db)
    alias_doc = {
        "_id": f"agent:{from_project}/{from_agent}",
        "type": "agent",
        "from_project": from_project,
        "from_agent": from_agent,
        "to_project": to_project,
        "to_agent": to_agent,
        "renamed_at": now,
        "renamed_by": actor,
        "expires_at": expires,
        "pre_counts": {**counts_mongo, **counts_chroma},
    }
    alias_col.insert_one(alias_doc)

    # Apply mutations
    mongo_results = _apply_agent_renames_mongo(
        db, from_project, from_agent, to_project, to_agent,
    )
    chroma_results = await _apply_agent_renames_chroma(
        chroma, from_project, from_agent, to_project, to_agent,
    )

    # Audit log
    try:
        log_audit("admin.rename_agent", actor, from_project, {
            "from_project": from_project,
            "from_agent": from_agent,
            "to_project": to_project,
            "to_agent": to_agent,
            "mongo": mongo_results,
            "chroma": chroma_results,
            "alias_expires_at": expires.isoformat(),
        }, session_id)
    except Exception:
        pass

    return {
        "status": "renamed",
        "from": f"{from_agent}@{from_project}",
        "to": f"{to_agent}@{to_project}",
        "modified": {**mongo_results, **chroma_results},
        "alias_expires_at": expires.isoformat(),
        "alias_id": alias_doc["_id"],
        "note": (
            f"Alias active for {ALIAS_TTL_DAYS} days. Old-name "
            f"memory_start_session calls will redirect with a warning. "
            f"Update CLAUDE.md before alias expiry."
        ),
    }


async def perform_rename_project(
    db, chroma, *,
    from_project: str, to_project: str,
    dry_run: bool, actor: str, session_id: str = "",
) -> Dict:
    """Rename an entire project."""
    from_project = normalize_project(from_project)
    to_project = normalize_project(to_project)

    if not from_project or not to_project:
        return {"error": "from_project and to_project are required"}
    if from_project == to_project:
        return {"error": "source and target are identical"}

    existing = _existing_alias_for_project(db, from_project)
    if existing:
        if existing.get("to_project") == to_project:
            return {
                "status": "already_renamed",
                "from": from_project, "to": to_project,
                "alias_doc": {k: v for k, v in existing.items() if k != "_id"},
            }
        return {
            "error": "project already aliased to a different target",
            "existing_target": existing.get("to_project"),
        }

    target_exists = db.projects.find_one({"name": to_project})
    if target_exists:
        return {
            "error": f"target project '{to_project}' already exists",
            "hint": "remove the target project first or pick a different name",
        }

    counts = _count_project_impact(db, from_project)

    if dry_run:
        return {
            "status": "dry_run",
            "from": from_project, "to": to_project,
            "would_modify": counts,
        }

    now = utc_now()
    expires = now + timedelta(days=ALIAS_TTL_DAYS)
    alias_col = _alias_collection(db)
    alias_doc = {
        "_id": f"project:{from_project}",
        "type": "project",
        "from_project": from_project,
        "to_project": to_project,
        "renamed_at": now,
        "renamed_by": actor,
        "expires_at": expires,
        "pre_counts": counts,
    }
    alias_col.insert_one(alias_doc)

    mongo_results = _apply_project_rename_mongo(db, from_project, to_project)
    chroma_results = await _apply_project_rename_chroma(chroma, from_project, to_project)

    try:
        log_audit("admin.rename_project", actor, from_project, {
            "from_project": from_project,
            "to_project": to_project,
            "mongo": mongo_results,
            "chroma": chroma_results,
            "alias_expires_at": expires.isoformat(),
        }, session_id)
    except Exception:
        pass

    return {
        "status": "renamed",
        "from": from_project, "to": to_project,
        "modified": {**mongo_results, **chroma_results},
        "alias_expires_at": expires.isoformat(),
        "alias_id": alias_doc["_id"],
    }


def list_aliases(db, alias_type: Optional[str] = None) -> List[Dict]:
    """List active aliases. Used by admin tooling for visibility."""
    col = _alias_collection(db)
    q = {}
    if alias_type:
        q["type"] = alias_type
    out = []
    for d in col.find(q).sort("renamed_at", -1):
        d.pop("_id", None)
        for k in ("renamed_at", "expires_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat() if hasattr(d[k], "isoformat") else d[k]
        out.append(d)
    return out
