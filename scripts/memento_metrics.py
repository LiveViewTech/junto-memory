#!/usr/bin/env python3
"""Usage + success metrics for the Memento features (1.42.0).

    snapshot [--date YYYY-MM-DD]   one metrics_daily row for a UTC day (default
                                   yesterday). Idempotent: re-running a day
                                   replaces its row. Cron nightly.
    report [--since YYYY-MM-DD] [--json PATH]
                                   totals over metrics_daily since a date
                                   (default the 1.42.0 deploy) plus live
                                   corpus state, printed as markdown.

Read-only against the knowledge base; the only write is metrics_daily.
Runs on the host like facets_backfill.py: pymongo to localhost:27019 with
.env creds, chromadb to localhost:8001.

What each number means: design:memento-metrics-v0.
"""

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

DEPLOYED = "2026-10-30"          # 1.42.0 live on sage
# Chroma docs (proj_* + shared_*) holding a credential at the 1.42.0 deploy,
# same scope as report()'s rescan. (The first dry-run's 95/34,108 also covered
# messages + Mongo specs: 81 Chroma + 14 messages.)
BASELINE = {
    "docs_scanned": 27395,
    "docs_with_credentials": 84,
}
STALE_VOLATILE_DAYS = 21         # one volatile half-life (interface:ranking-decay-v0)
EXTRACTOR = "extractor"


def _env():
    env = {}
    for line in open(REPO / ".env"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


def _mongo():
    from pymongo import MongoClient
    env = _env()
    client = MongoClient(
        "localhost", int(env.get("MONGO_PORT", 27019)),
        username=env["MONGO_USER"], password=env["MONGO_PASSWORD"],
        authSource="admin", directConnection=True,
    )
    return client[env.get("MONGO_DB", "mcp_orchestrator")]


def _parse_ts(s):
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def _corpus(with_documents=False):
    """{doc_id: meta} for every doc in proj_* and shared_* collections.
    With with_documents, also count docs that still hold a credential."""
    import chromadb

    from shared_memory.redact import redact_secrets
    chroma = await chromadb.AsyncHttpClient(host="localhost", port=8001)
    metas, with_secret = {}, 0
    for c in await chroma.list_collections():
        name = c.name if hasattr(c, "name") else c
        if not (name.startswith("proj_") or name.startswith("shared_")):
            continue
        col = await chroma.get_collection(name)
        off = 0
        while True:
            inc = ["metadatas", "documents"] if with_documents else ["metadatas"]
            got = await col.get(limit=2000, offset=off, include=inc)
            if not got["ids"]:
                break
            docs = got.get("documents") or [None] * len(got["ids"])
            for i, m, d in zip(got["ids"], got["metadatas"], docs):
                metas[i] = m or {}
                if with_documents and d and redact_secrets(d)[1]:
                    with_secret += 1
            off += 2000
    return metas, with_secret


def _is_extractor(meta):
    return meta.get("claude_instance") == EXTRACTOR or "transcript-extract" in (meta.get("tags") or "")


def _served_stats(rows, metas, shelf):
    """rows: recall_events / query_events docs. Counts what was served."""
    out = Counter()
    for ev in rows:
        out["events"] += 1
        ts = ev.get("ts")
        ts = ts if (ts and ts.tzinfo) else (ts.replace(tzinfo=timezone.utc) if ts else None)
        for doc_id in ev.get("ids") or []:
            out["served"] += 1
            m = metas.get(doc_id, {})
            v = m.get("verification") or "unreviewed"
            out[f"served_{v}"] += 1
            if _is_extractor(m):
                out["served_extractor"] += 1
            if shelf.get(doc_id) == "volatile":
                out["served_volatile"] += 1
                upd = _parse_ts(m.get("updated") or m.get("created"))
                if ts and upd and (ts - upd).days > STALE_VOLATILE_DAYS:
                    out["served_stale_volatile"] += 1
    return dict(out)


async def snapshot(db, day):
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    rng = {"$gte": start, "$lt": end}
    metas, _ = await _corpus()
    shelf = {r["_id"]: r.get("shelf_life") for r in db.learning_facets.find({}, {"shelf_life": 1})}

    red = list(db.redaction_events.find({"ts": rng}))
    red_kind, red_tool = Counter(), Counter()
    for ev in red:
        red_tool[ev.get("tool")] += 1
        for h in ev.get("hits") or []:
            red_kind[h.get("kind")] += 1

    ver = list(db.verification_events.find({"at": {"$gte": start.isoformat(), "$lt": end.isoformat()}}))
    states = Counter((m.get("verification") or "unreviewed") for m in metas.values())
    ext = {i: m for i, m in metas.items() if _is_extractor(m)}
    ext_new = sum(1 for m in ext.values()
                  if (t := _parse_ts(m.get("created"))) and start <= t < end)

    row = {
        "_id": day.isoformat(),
        "computed_at": datetime.now(timezone.utc),
        "redaction": {"writes": len(red), "hits_by_kind": dict(red_kind), "writes_by_tool": dict(red_tool)},
        "verification": {
            "events_by_verdict": dict(Counter(e.get("to") for e in ver)),
            "events_by_actor": dict(Counter(e.get("by") for e in ver)),
            "corpus_states": dict(states),
        },
        "recall": _served_stats(db.recall_events.find({"ts": rng}, {"ids": 1, "ts": 1}), metas, shelf),
        "recall_pulls": db.recall_pulls.count_documents({"ts": rng}),
        "query": _served_stats(db.query_events.find({"ts": rng}, {"ids": 1, "ts": 1}), metas, shelf),
        "extraction": {
            "new_rows": ext_new,
            "total_rows": len(ext),
            "states": dict(Counter((m.get("verification") or "unreviewed") for m in ext.values())),
        },
        "decay": {
            "projects_on": sorted(r.get("scope") for r in db.query_config.find({"shelf_life_decay": True}, {"scope": 1})),
        },
        "corpus_docs": len(metas),
    }
    db.metrics_daily.replace_one({"_id": row["_id"]}, row, upsert=True)
    return row


def _sum(rows, path):
    total = Counter()
    for r in rows:
        node = r
        for p in path:
            node = (node or {}).get(p) or {}
        if isinstance(node, dict):
            total.update({k: v for k, v in node.items() if isinstance(v, (int, float))})
    return dict(total)


async def report(db, since, json_path=None, eval_path=None):
    rows = list(db.metrics_daily.find({"_id": {"$gte": since}}).sort("_id", 1))
    metas, with_secret = await _corpus(with_documents=True)
    ext = [m for m in metas.values() if _is_extractor(m)]
    ext_states = Counter((m.get("verification") or "unreviewed") for m in ext)
    states = Counter((m.get("verification") or "unreviewed") for m in metas.values())
    recall = _sum(rows, ["recall"])
    query = _sum(rows, ["query"])
    rep = {
        "window": {"since": since, "days": len(rows), "through": rows[-1]["_id"] if rows else None},
        "redaction": {
            "writes_redacted": sum((r.get("redaction") or {}).get("writes", 0) for r in rows),
            "hits_by_kind": _sum(rows, ["redaction", "hits_by_kind"]),
            "writes_by_tool": _sum(rows, ["redaction", "writes_by_tool"]),
            "baseline_docs_with_credentials": BASELINE["docs_with_credentials"],
            "baseline_docs_scanned": BASELINE["docs_scanned"],
            "now_docs_with_credentials": with_secret,
            "now_docs_scanned": len(metas),
        },
        "verification": {
            "verdicts": _sum(rows, ["verification", "events_by_verdict"]),
            "actors": _sum(rows, ["verification", "events_by_actor"]),
            "corpus_states": dict(states),
        },
        "serving": {"recall": recall, "query": query,
                    "recall_pulls": sum(r.get("recall_pulls", 0) for r in rows)},
        "extraction": {"rows": len(ext), "states": dict(ext_states),
                       "served_recall": recall.get("served_extractor", 0),
                       "served_query": query.get("served_extractor", 0)},
        "decay": {"projects_on": (rows[-1].get("decay") or {}).get("projects_on") if rows else [],
                  "eval": json.loads(Path(eval_path).read_text()) if eval_path and Path(eval_path).exists() else None},
    }
    if json_path:
        Path(json_path).write_text(json.dumps(rep, indent=2, default=str))
    _print_markdown(rep)
    return rep


def _pct(a, b):
    return f"{100.0 * a / b:.2f}%" if b else "n/a"


def _print_markdown(r):
    w, red, ver, srv, ext, dec = (r[k] for k in ("window", "redaction", "verification", "serving", "extraction", "decay"))
    rq = srv["recall"]
    print(f"# Memento features: {w['since']} → {w['through']} ({w['days']} days of snapshots)\n")
    print("## #3 Secret redaction")
    print(f"- Writes with secrets removed: **{red['writes_redacted']}**  by kind {red['hits_by_kind']}  by tool {red['writes_by_tool']}")
    print(f"- Stored docs holding a credential: baseline {red['baseline_docs_with_credentials']}/{red['baseline_docs_scanned']} "
          f"({_pct(red['baseline_docs_with_credentials'], red['baseline_docs_scanned'])}) → now "
          f"{red['now_docs_with_credentials']}/{red['now_docs_scanned']} ({_pct(red['now_docs_with_credentials'], red['now_docs_scanned'])})\n")
    print("## #1 Verification")
    print(f"- Verdicts in window: {ver['verdicts']}  by {ver['actors']}")
    print(f"- Corpus states now: {ver['corpus_states']}")
    print(f"- Recall snippets served from verified docs: {rq.get('served_verified', 0)}/{rq.get('served', 0)}\n")
    print("## #2 Shelf-life decay")
    print(f"- Projects with the flag on: {dec['projects_on'] or 'none'}")
    print(f"- Recall snippets that were stale volatile (>{STALE_VOLATILE_DAYS}d): "
          f"{rq.get('served_stale_volatile', 0)}/{rq.get('served', 0)} ({_pct(rq.get('served_stale_volatile', 0), rq.get('served', 0))})")
    if dec.get("eval"):
        e = dec["eval"]
        print(f"- Replay eval (n={e.get('n')}): top-3 changed on {e.get('changed')}; graded on/off/tie = "
              f"{e.get('wins_on')}/{e.get('wins_off')}/{e.get('ties')}")
    print()
    print("## #4 Transcript extraction")
    print(f"- Extracted rows: {ext['rows']}  states {ext['states']}")
    print(f"- Served: recall {ext['served_recall']}, query {ext['served_query']}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--date")
    s.add_argument("--backfill-from", help="snapshot every day from this date through --date")
    r = sub.add_parser("report")
    r.add_argument("--since", default=DEPLOYED)
    r.add_argument("--json")
    r.add_argument("--eval", help="decay replay eval result JSON")
    args = ap.parse_args()
    db = _mongo()
    if args.cmd == "snapshot":
        day = (datetime.fromisoformat(args.date).date() if args.date
               else (datetime.now(timezone.utc) - timedelta(days=1)).date())
        first = datetime.fromisoformat(args.backfill_from).date() if args.backfill_from else day
        d = first
        while d <= day:
            row = asyncio.run(snapshot(db, d))
            print(json.dumps({"date": row["_id"], "recall": row["recall"], "redaction": row["redaction"]["writes"]}, default=str))
            d += timedelta(days=1)
    else:
        asyncio.run(report(db, args.since, args.json, args.eval))


if __name__ == "__main__":
    main()
