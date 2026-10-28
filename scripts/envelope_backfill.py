#!/usr/bin/env python3
"""Envelope-leak BACK-CATALOGUE sweep + backfill (backlog_1115f9fe35f7).

The write-time lint (write_lint.recover_envelope_leak) stops NEW leaks; it was
never a history sweep. This walks every chroma project collection + shared_patterns,
RE-RUNS the census with the CURRENT detector, and (only with --commit) applies the
same strip+re-route recovery to stored bodies, preserving ids.

⚠️ DETECTOR-CENSUS TRAP (learning_182decf2958202a8): the 2026-08-10 census of 527
used a looser envelope-tail rule. The tail-guard was tightened 2026-08-26
(func_098f262d0879) + 0c82a3d to require an envelope-specific token after a field's
closing tag, so the CURRENT strip_envelope_leak no longer fires on the THIRD emission
shape (bare `</details>\\n<project>nimbus</project><tags>…`). This sweep therefore
measures TWO populations and reports the gap:
  - writelint : what the shipped strip_envelope_leak catches today (the fixable-by-
                the-live-recovery-path set).
  - raw       : superset — any unambiguous tool-call envelope token in the body, plus
                the bare <project>/<tags> tail shape. raw \\ writelint = detector gap.

Modes:
  (default)   dry-run: census + write leaked-doc detail to --out, mutate NOTHING.
  --commit    apply recovery to the WRITELINT set (Tom-gated; bulk UPDATE on the
              live corpus incl. other teams' collections — do not run without an OK).
  --project P restrict to one collection (proj_P; use 'shared_patterns' literally).
  --out PATH  where to write the dry-run detail JSON (default scratchpad).

Host-side, like facets_backfill.py: chromadb AsyncHttpClient localhost:8001.
Reuses src/shared_memory/write_lint.py verbatim so a committed fix is byte-identical
to what the write-time path would have produced.
"""

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from shared_memory.write_lint import (  # noqa: E402
    _ENVELOPE_FIELDS,
    recover_envelope_leak,
    strip_envelope_leak,
)

# Unambiguous tool-call envelope tokens — no legitimate doc body contains these.
_RAW_ENVELOPE_RE = re.compile(
    r'<parameter\s+name="|</?(?:antml:)?invoke\b|</?(?:antml:)?function_calls\b',
    re.IGNORECASE,
)
# Bare-tag leak shape (census "third shape"): an envelope FIELD close followed by a
# bare <project>/<tags>/<files_modified> tag — the record_learning/end_session tail
# that the tightened tail-guard deliberately no longer treats as a leak.
_BARE_TAG_TAIL_RE = re.compile(r"<(?:project|tags|files_modified)>", re.IGNORECASE)
_FIELD_CLOSE_RE = re.compile(
    r"</(?:" + "|".join(_ENVELOPE_FIELDS) + r")>", re.IGNORECASE
)

DEFAULT_OUT = (
    "/tmp/claude-1000/-home-tlemmons-sharedUtils-junto-junto-memory/"
    "fa6633e3-3265-453a-a6fa-dd51247c75ed/scratchpad/envelope_sweep.json"
)


async def _chroma():
    import chromadb

    return await chromadb.AsyncHttpClient(host="localhost", port=8001)


async def _target_collections(chroma, project=None):
    names = [
        c.name if hasattr(c, "name") else c for c in await chroma.list_collections()
    ]
    if project:
        want = project if project.startswith(("proj_", "shared_")) else f"proj_{project}"
        return [n for n in names if n == want]
    return sorted(n for n in names if n.startswith("proj_")) + [
        n for n in names if n == "shared_patterns"
    ]


def _raw_leak(body: str):
    """Superset detector. Returns (is_leak, shape) where shape is
    'writelint-token' | 'bare-tag' | None. Independent of strip_envelope_leak."""
    if not body:
        return False, None
    if _RAW_ENVELOPE_RE.search(body):
        return True, "writelint-token"
    m = _FIELD_CLOSE_RE.search(body)
    if m:
        tail = body[m.end():]
        # only the FIRST few chars after the field close, to avoid matching a
        # <project> tag discussed far later in a prose body
        if _BARE_TAG_TAIL_RE.match(tail.lstrip()):
            return True, "bare-tag"
    return False, None


def _month(meta):
    c = (meta or {}).get("created") or (meta or {}).get("timestamp") or ""
    return c[:7] if isinstance(c, str) and len(c) >= 7 else "unknown"


async def sweep(chroma, project=None, out_path=DEFAULT_OUT, commit=False):
    cols = await _target_collections(chroma, project)
    census = {
        "writelint": Counter(),  # per-collection: catchable by live recovery path
        "raw": Counter(),  # per-collection: superset
        "gap": Counter(),  # raw-only (detector blind spot)
    }
    by_month = Counter()
    shape_counts = Counter()
    detail = []  # writelint-catchable leaked docs (the backfill set)
    gap_detail = []  # raw-only docs (detector gap — NOT auto-fixable)
    committed = 0

    for name in cols:
        col = await chroma.get_collection(name)
        got = await col.get(include=["documents", "metadatas"])
        ids = got.get("ids") or []
        docs = got.get("documents") or []
        metas = got.get("metadatas") or []
        for i, doc_id in enumerate(ids):
            body = docs[i] if i < len(docs) else None
            meta = (metas[i] if i < len(metas) else {}) or {}
            if (meta.get("status") or "active") != "active":
                continue
            if not body:
                continue

            clean, extracted, wl_leaked = strip_envelope_leak(body, "content")
            raw_leaked, shape = _raw_leak(body)

            if wl_leaked:
                census["writelint"][name] += 1
                by_month[_month(meta)] += 1
                shape_counts[shape or "writelint"] += 1
                _, rec_project, notes = recover_envelope_leak(body, "content")
                reroute = bool(rec_project) and rec_project != _proj_of(name)
                detail.append(
                    {
                        "collection": name,
                        "id": doc_id,
                        "title": (meta.get("title") or "")[:120],
                        "created": meta.get("created") or meta.get("timestamp"),
                        "type": meta.get("type"),
                        "recovered_project": rec_project,
                        "reroute_from_shared": name == "shared_patterns" and reroute,
                        "extracted_params": list(extracted.keys()),
                        "tail_snippet": body[max(0, len(clean) - 0):][:240],
                        "notes": notes,
                    }
                )
                if commit:
                    committed += await _apply(col, doc_id, body, meta)
            if raw_leaked:
                census["raw"][name] += 1
                if not wl_leaked:
                    census["gap"][name] += 1
                    shape_counts[f"gap:{shape}"] += 1
                    gap_detail.append(
                        {
                            "collection": name,
                            "id": doc_id,
                            "title": (meta.get("title") or "")[:120],
                            "created": meta.get("created") or meta.get("timestamp"),
                            "shape": shape,
                            "tail_snippet": _tail_after_field(body)[:240],
                        }
                    )

    report = {
        "mode": "commit" if commit else "dry-run",
        "collections_scanned": cols,
        "writelint_total": sum(census["writelint"].values()),
        "raw_total": sum(census["raw"].values()),
        "gap_total": sum(census["gap"].values()),
        "writelint_by_collection": dict(census["writelint"]),
        "raw_by_collection": dict(census["raw"]),
        "gap_by_collection": dict(census["gap"]),
        "writelint_by_month": dict(sorted(by_month.items())),
        "shape_counts": dict(shape_counts),
        "committed": committed,
        "backfill_set": detail,
        "detector_gap_set": gap_detail,
    }
    Path(out_path).write_text(json.dumps(report, indent=2, default=str))

    print(f"== envelope sweep ({report['mode']}) ==")
    print(f"collections: {', '.join(cols)}")
    print(f"WRITELINT-catchable (backfill set): {report['writelint_total']}")
    for k, v in sorted(census["writelint"].items()):
        print(f"    {k}: {v}")
    print(f"RAW superset:                       {report['raw_total']}")
    print(f"DETECTOR GAP (raw-only, NOT auto-fixable): {report['gap_total']}")
    for k, v in sorted(census["gap"].items()):
        print(f"    {k}: {v}")
    print(f"by-month (writelint): {report['writelint_by_month']}")
    print(f"shapes: {report['shape_counts']}")
    if commit:
        print(f"COMMITTED updates: {committed}")
    print(f"detail written: {out_path}")
    return report


def _proj_of(collection_name):
    if collection_name.startswith("proj_"):
        return collection_name[len("proj_"):]
    return None


def _tail_after_field(body):
    m = _FIELD_CLOSE_RE.search(body or "")
    return body[m.start():] if m else (body or "")


async def _apply(col, doc_id, body, meta):
    """Commit path: strip + re-route via the SAME recovery the write path uses,
    then upsert the cleaned body back under the same id. Returns 1 if changed."""
    clean, rec_project, _notes = recover_envelope_leak(body, "content")
    if clean == body:
        return 0
    # In-place body fix only. Cross-collection MOVE (misfiled shared->proj) is a
    # SEPARATE, owner-notified step — deliberately NOT done here.
    await col.update(ids=[doc_id], documents=[clean])
    return 1


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=None)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    chroma = await _chroma()
    await sweep(chroma, args.project, args.out, args.commit)


if __name__ == "__main__":
    asyncio.run(main())
