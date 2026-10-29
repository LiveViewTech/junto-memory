"""Stale action obligations lapse + send-side hints (design:action-lapse-v0)."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import shared_memory.action_lapse as al
import shared_memory.tools.messaging as m
from shared_memory.config import classify_lane

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)


def _match(doc, query):
    for k, cond in query.items():
        if k == "$or":
            if not any(_match(doc, q) for q in cond):
                return False
            continue
        v = doc.get(k)
        if isinstance(cond, dict):
            for op, arg in cond.items():
                if op == "$in" and v not in arg:
                    return False
                if op == "$ne" and v == arg:
                    return False
                if op == "$lt" and not (v is not None and v < arg):
                    return False
                if op == "$gte" and not (v is not None and v >= arg):
                    return False
        elif v != cond:
            return False
    return True


class _Cursor(list):
    def sort(self, spec):
        key, direction = spec[0]
        return _Cursor(sorted(self, key=lambda d: d.get(key), reverse=direction < 0))

    def limit(self, n):
        return _Cursor(self[:n])


class _Res:
    def __init__(self, n):
        self.modified_count = self.matched_count = n


class _Messages:
    def __init__(self, docs):
        self.docs = {d["_id"]: d for d in docs}

    def find(self, query, projection=None):
        return _Cursor(dict(d) for d in self.docs.values() if _match(d, query))

    def find_one(self, query, projection=None):
        return next((dict(d) for d in self.docs.values() if _match(d, query)), None)

    def distinct(self, field, query):
        return list({d.get(field) for d in self.docs.values() if _match(d, query)})

    def update_one(self, query, change):
        for d in self.docs.values():
            if _match(d, query):
                d.update(change.get("$set", {}))
                for k in change.get("$unset", {}):
                    d.pop(k, None)
                return _Res(1)
        return _Res(0)

    def insert_one(self, doc):
        self.docs[doc["_id"]] = doc


class _DB:
    def __init__(self, docs):
        self.messages = _Messages(docs)


def _msg(mid, days_old, **kw):
    base = {"_id": mid, "to_instance": "coordinator", "to_project": "nimbus",
            "from_instance": "server-team", "from_project": "nimbus",
            "category": "task", "status": "pending", "obligation": "open",
            "message": "please do the thing " + mid, "subject": None,
            "created_at": NOW - timedelta(days=days_old)}
    base.update(kw)
    return base


def _sweep(db, monkeypatch):
    sent = []

    async def _fake_digest(db_, project, agent, body, n, days):
        sent.append((project, agent, n, body))
        return "msg_digest"

    monkeypatch.setattr(al, "_send_digest", _fake_digest)
    res = asyncio.run(al.sweep_project(db, "nimbus", 30, now=NOW))
    return res, sent


def test_lapses_old_open_and_responded_only(monkeypatch):
    db = _DB([
        _msg("old_open", 45),
        _msg("old_resp", 90, obligation="responded"),
        _msg("young", 5),
        _msg("resolved", 90, obligation="resolved"),
        _msg("info", 90, category="info", obligation=None),
        _msg("acked", 90, status="received"),
    ])
    res, sent = _sweep(db, monkeypatch)
    d = db.messages.docs
    assert res == {"coordinator": 2}
    assert d["old_open"]["obligation"] == "lapsed" and d["old_open"]["lapsed_from"] == "open"
    assert d["old_resp"]["lapsed_from"] == "responded"
    assert d["young"]["obligation"] == "open"
    assert d["resolved"]["obligation"] == "resolved"
    assert d["acked"]["obligation"] == "open"
    assert len(sent) == 1 and sent[0][2] == 2


def test_human_exemptions(monkeypatch):
    db = _DB([
        _msg("from_tom", 60, sent_by_human=True),
        _msg("needs_human", 60, require_human=True),
        _msg("tom_replied", 60),
        _msg("tom_reply", 10, _id="reply1", in_response_to="tom_replied",
             sent_by_human=True, category="info", obligation=None, to_instance="server-team"),
        _msg("stale_tom_reply_parent", 90),
        _msg("old_reply", 70, _id="reply2", in_response_to="stale_tom_reply_parent",
             sent_by_human=True, category="info", obligation=None, to_instance="server-team"),
    ])
    res, _ = _sweep(db, monkeypatch)
    d = db.messages.docs
    assert d["from_tom"]["obligation"] == "open"
    assert d["needs_human"]["obligation"] == "open"
    assert d["tom_replied"]["obligation"] == "open"      # human reply within 30d
    assert d["stale_tom_reply_parent"]["obligation"] == "lapsed"  # human reply too old
    assert res == {"coordinator": 1}


def test_broadcast_never_lapsed(monkeypatch):
    db = _DB([_msg("bcast", 90, to_instance="*")])
    res, _ = _sweep(db, monkeypatch)
    assert res == {} and db.messages.docs["bcast"]["obligation"] == "open"


def test_digest_is_bounded_and_grouped():
    items = [_msg("m%03d" % i, 40 + i, from_instance="pipeline" if i % 2 else "frames-team")
             for i in range(70)]
    body = al.build_digest(items, 30)
    assert "70 action message(s)" in body
    assert "pipeline@nimbus 35" in body and "frames-team@nimbus 35" in body
    assert body.count("[task]") == al.DIGEST_MAX_ITEMS
    assert "(+40 older not listed)" in body
    assert "m000" in body and "m069" not in body  # newest first


def test_lapsed_lane():
    assert classify_lane("task", "lapsed") == ("lapsed", None)
    assert classify_lane("task", "open") == ("action", 0)


def test_send_hints_size_and_possible_parent():
    db = _DB([
        _msg("ask1", 3, from_instance="coordinator", to_instance="server-team",
             category="question", subject="which schema for invoices?"),
        _msg("ask2", 1, from_instance="coordinator", to_instance="server-team"),
        _msg("done", 1, from_instance="coordinator", to_instance="server-team",
             obligation="resolved"),
    ])
    h = m._send_hints(db, "x" * 3500, "coordinator", "nimbus", "server-team", "nimbus", None)
    assert "3500 chars" in h["size_hint"]
    assert [p["id"] for p in h["possible_parent"]] == ["ask2", "ask1"]
    assert h["possible_parent"][1]["preview"] == "which schema for invoices?"
    # A threaded reply, a short body, or a broadcast gets no hints.
    assert m._send_hints(db, "ok", "coordinator", "nimbus", "server-team", "nimbus", "ask1") == {}
    assert m._send_hints(db, "ok", "*", "nimbus", "server-team", "nimbus", None) == {}


def test_revive_and_manual_lapse(monkeypatch):
    db = _DB([_msg("x", 60, obligation="lapsed", lapsed_at=NOW, lapsed_from="open")])
    monkeypatch.setattr(m, "get_mongo", lambda: db)
    monkeypatch.setattr(m, "require_session", lambda sid: None)
    r = json.loads(asyncio.run(m.memory_update_message_status("s", "x", "open")))
    assert r["obligation"] == "open"
    assert db.messages.docs["x"]["obligation"] == "open" and "lapsed_at" not in db.messages.docs["x"]
    r = json.loads(asyncio.run(m.memory_update_message_status("s", "x", "lapsed")))
    assert db.messages.docs["x"]["obligation"] == "lapsed"
    assert db.messages.docs["x"]["lapsed_from"] == "open"


def test_responded_lapse_uses_reply_age(monkeypatch):
    db = _DB([
        _msg("resp_old_reply", 20, obligation="responded", responded_at=NOW - timedelta(days=10)),
        _msg("resp_fresh_reply", 20, obligation="responded", responded_at=NOW - timedelta(days=2)),
        _msg("resp_no_ts", 9, obligation="responded"),
        _msg("open_20d", 20),
    ])
    sent = []

    async def _fake_digest(db_, project, agent, body, n, days):
        sent.append(n)

    monkeypatch.setattr(al, "_send_digest", _fake_digest)
    res = asyncio.run(al.sweep_project(db, "nimbus", 30, now=NOW, responded_days=7))
    d = db.messages.docs
    assert d["resp_old_reply"]["obligation"] == "lapsed"
    assert d["resp_fresh_reply"]["obligation"] == "responded"
    assert d["resp_no_ts"]["obligation"] == "lapsed"   # falls back to created_at
    assert d["open_20d"]["obligation"] == "open"       # open still waits 30d
    assert res == {"coordinator": 2}
