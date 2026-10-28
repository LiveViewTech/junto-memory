"""Query tool defaults — `backlog_6d5aa1a2849f`.

Per-project overridable defaults for memory_query's preview vs. full-content
behavior. Same shape as push_control_config (default scope + per-project
override). Operator-tier write via memory_admin; callers always win via
explicit `expand` / `expand_top` / `snippet_length` params.

Rollout model: ship with `default_expand=True` (current behavior unchanged).
Flip per-project to `default_expand=False` when that project's callers are
ready for previews. No global cutover required.
"""

import logging
from typing import Any, Dict, Optional

from shared_memory.audit import log_audit
from shared_memory.helpers import normalize_project, utc_now

log = logging.getLogger(__name__)


# Defaults applied when no DB doc exists for either scope.
DEFAULT_EXPAND = True
DEFAULT_EXPAND_TOP = 0
DEFAULT_SNIPPET_LENGTH = 200
# POST /recall similarity floor (interface:recall-v0 §Floor). Ships
# CONSERVATIVE (cry-wolf-averse): deliberately above memory_query's
# MIN_RELEVANCE_THRESHOLD=0.3. Lower per-project only on A2(ii)
# pull-through evidence.
DEFAULT_RECALL_FLOOR = 0.6

# Lexical identifier lane (lexical.py): max exact-match docs added per
# memory_query / recall request. 0 disables the lane.
DEFAULT_LEXICAL_MAX = 2

# Chunked arctic2 lane (chunk_index.py, interface:chunk-index-v0). OFF by
# default; its scores are cosine of the best chunk — NOT the MiniLM scale —
# so it carries its own floors. Calibrated 2026-10-25 on eval v0+v1 to pass
# the SAME share of null queries (question vs a collection without the
# answer) as MiniLM's 0.3 / 0.6: 87% / 2% null pass, while true positives
# pass 100% / 50% (MiniLM: 99% / 38%). interface:chunk-index-v0 0.2.0.
DEFAULT_CHUNK_INDEX = False
DEFAULT_CHUNK_MIN_RELEVANCE = 0.37
DEFAULT_CHUNK_RECALL_FLOOR = 0.63

# Cross-encoder rerank for memory_query (rerank.py, interface:rerank-v0).
DEFAULT_RERANK = False
DEFAULT_RERANK_MIN = 0.05  # top-3 rows scoring 0.02-0.5 were still mostly useful (Claude-graded, 10-25); see interface:rerank-v0

# Allowed config keys + their types (used by set_config_value).
CONFIG_KEYS = {
    "default_expand": bool,
    "default_expand_top": int,
    "default_snippet_length": int,
    "recall_floor": float,
    "lexical_max": int,
    "chunk_index": bool,
    "chunk_min_relevance": float,
    "chunk_recall_floor": float,
    "rerank": bool,
    "rerank_min": float,
}

DEFAULT_SCOPE = "__default__"


def init_query_config_indexes(db) -> None:
    """Register indexes on the query_config collection. Idempotent."""
    if db is None:
        return
    col = db.query_config
    col.create_index("scope", unique=True)
    col.create_index("project")


def _default_config_dict() -> Dict[str, Any]:
    return {
        "default_expand": DEFAULT_EXPAND,
        "default_expand_top": DEFAULT_EXPAND_TOP,
        "default_snippet_length": DEFAULT_SNIPPET_LENGTH,
        "recall_floor": DEFAULT_RECALL_FLOOR,
        "lexical_max": DEFAULT_LEXICAL_MAX,
        "chunk_index": DEFAULT_CHUNK_INDEX,
        "chunk_min_relevance": DEFAULT_CHUNK_MIN_RELEVANCE,
        "chunk_recall_floor": DEFAULT_CHUNK_RECALL_FLOOR,
        "rerank": DEFAULT_RERANK,
        "rerank_min": DEFAULT_RERANK_MIN,
    }


def _read_config_doc(db, scope: str) -> Dict[str, Any]:
    if db is None:
        return {}
    try:
        return db.query_config.find_one({"scope": scope}) or {}
    except Exception:
        return {}


def get_effective_config(db, project: Optional[str] = None) -> Dict[str, Any]:
    """Return effective query-tool defaults: code default + server doc + project override.

    Includes `_sources` map (per-key) for transparency in the admin get view.
    """
    base = _default_config_dict()
    sources: Dict[str, str] = {k: "code_default" for k in base}

    db_default = _read_config_doc(db, DEFAULT_SCOPE)
    for k in list(base.keys()):
        if k in db_default and db_default[k] is not None:
            base[k] = db_default[k]
            sources[k] = "server_default"

    if project:
        norm = normalize_project(project)
        override = _read_config_doc(db, norm)
        for k in list(base.keys()):
            if k in override and override[k] is not None:
                base[k] = override[k]
                sources[k] = f"project:{norm}"

    base["_sources"] = sources
    base["_project"] = normalize_project(project) if project else None
    return base


def set_config_value(db, project: Optional[str], key: str, value: Any, actor: str) -> Dict[str, Any]:
    """Upsert one query-config key. project=None writes the server default."""
    if key not in CONFIG_KEYS:
        return {"error": f"unknown config key '{key}'; valid keys: {sorted(CONFIG_KEYS)}"}

    declared_type = CONFIG_KEYS[key]
    if value is not None:
        # bool is a subclass of int — guard the bool case explicitly so a raw int doesn't get accepted.
        if declared_type is bool:
            if not isinstance(value, bool):
                return {"error": f"value for {key!r} must be bool"}
        else:
            try:
                value = declared_type(value)
            except (TypeError, ValueError):
                return {"error": f"value for {key!r} must be {declared_type.__name__}"}

    # Domain validation
    if key == "default_snippet_length" and isinstance(value, int):
        if value < 50 or value > 2000:
            return {"error": "default_snippet_length must be between 50 and 2000"}
    if key == "default_expand_top" and isinstance(value, int):
        if value < 0 or value > 50:
            return {"error": "default_expand_top must be between 0 and 50"}
    if key in ("recall_floor", "chunk_min_relevance", "chunk_recall_floor", "rerank_min") and isinstance(value, float):
        if value < 0.0 or value > 1.0:
            return {"error": f"{key} must be between 0.0 and 1.0"}
    if key == "lexical_max" and isinstance(value, int):
        if value < 0 or value > 5:
            return {"error": "lexical_max must be between 0 and 5"}

    if db is None:
        return {"error": "MongoDB unavailable"}

    scope = DEFAULT_SCOPE if project is None else normalize_project(project)
    now = utc_now()
    try:
        db.query_config.update_one(
            {"scope": scope},
            {"$set": {
                "scope": scope,
                "project": None if scope == DEFAULT_SCOPE else scope,
                key: value,
                "updated_at": now,
                "updated_by": actor,
            }},
            upsert=True,
        )
    except Exception as e:
        return {"error": f"write failed: {e}"}

    try:
        log_audit(
            "query_config.set",
            actor=actor,
            project=scope if scope != DEFAULT_SCOPE else "",
            details={"key": key, "value": value, "scope": scope},
        )
    except Exception:
        pass

    return {"ok": True, "scope": scope, "key": key, "value": value}


def reset_config(db, project: Optional[str], key: Optional[str], actor: str) -> Dict[str, Any]:
    """Drop a per-project override. project=None is invalid (server default cannot be reset)."""
    if project is None:
        return {"error": "reset_config requires a project (server default cannot be reset)"}
    if db is None:
        return {"error": "MongoDB unavailable"}

    scope = normalize_project(project)
    now = utc_now()
    if key is None:
        try:
            result = db.query_config.delete_one({"scope": scope})
        except Exception as e:
            return {"error": f"delete failed: {e}"}
        try:
            log_audit(
                "query_config.reset_all",
                actor=actor,
                project=scope,
                details={"scope": scope, "deleted_count": result.deleted_count},
            )
        except Exception:
            pass
        return {"ok": True, "scope": scope, "deleted": result.deleted_count > 0}

    if key not in CONFIG_KEYS:
        return {"error": f"unknown config key '{key}'"}

    try:
        result = db.query_config.update_one(
            {"scope": scope},
            {"$unset": {key: ""}, "$set": {"updated_at": now, "updated_by": actor}},
        )
    except Exception as e:
        return {"error": f"unset failed: {e}"}

    try:
        log_audit(
            "query_config.reset_key",
            actor=actor,
            project=scope,
            details={"scope": scope, "key": key, "matched": result.matched_count},
        )
    except Exception:
        pass

    return {"ok": True, "scope": scope, "key": key, "unset": result.matched_count > 0}
