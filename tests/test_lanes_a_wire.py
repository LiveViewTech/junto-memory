"""Lanes-A server wire shape (design:unified-messaging-v0 Stage 3,
interface:lanes-a-server-wire-v0 — coordinator-converged, inbox co-signed).

Pins the SERVER-OUTPUT half of lanes-A:
  - per-message `lane`/`tier`, server-computed via the single classify_lane()
    helper (computed-not-stored; no re-implementation client-side)
  - a two-tier WITHIN-PAGE display sort (action-open > action-responded >
    cleared/fyi; recency within tier; priority as final tiebreak ONLY)
  - top-level `lane_counts` counted over the FULL inbox, with the open/fyi
    watermark asymmetry (open actions counted regardless of read state; fyi
    counted unseen-only)

The load-bearing invariant (inbox's co-sign blocking condition, msg_83e884ecfac7):
the tier key is a POST-FETCH re-sort, never the DB .sort().limit() SELECTION key.
Selection stays created_at-primary (design:inbox-surfacing-v0 Fix A) so nothing
strands past limit() and the created_at cursor stays coherent — test_selection_
stays_recency_primary pins exactly that.
"""

import json
from datetime import timedelta

from shared_memory.helpers import utc_now


# ── Minimal Mongo query matcher (only the shapes these paths produce) ──
def _match(doc, query):
    for key, cond in query.items():
        if key == "$and":
            if not all(_match(doc, c) for c in cond):
                return False
        elif key == "$or":
            if not any(_match(doc, c) for c in cond):
                return False
        elif isinstance(cond, dict):
            val = doc.get(key)
            for op, operand in cond.items():
                if op == "$gt" and not (val is not None and val > operand):
                    return False
                elif op == "$lt" and not (val is not None and val < operand):
                    return False
                elif op == "$gte" and not (val is not None and val >= operand):
                    return False
                elif op == "$ne":
                    # array-aware: {field:{$ne:x}} excludes a doc whose array
                    # field contains x (read_by membership), not just scalar ==
                    if isinstance(val, list):
                        if operand in val:
                            return False
                    elif val == operand:
                        return False
                elif op == "$in" and val not in operand:
                    return False
                elif op == "$exists":
                    if (key in doc) != operand:
                        return False
        else:
            if doc.get(key) != cond:
                return False
    return True


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, spec):
        for field, direction in reversed(spec):
            self._docs.sort(
                key=lambda d: (d.get(field) is None, d.get(field)),
                reverse=(direction < 0),
            )
        return self

    def limit(self, n):
        return iter(self._docs[:n])


class _FakeMessages:
    def __init__(self, docs):
        self._docs = [dict(d) for d in docs]

    def find(self, query, projection=None):
        return _FakeCursor([d for d in self._docs if _match(d, query)])

    def count_documents(self, query):
        return sum(1 for d in self._docs if _match(d, query))

    def insert_one(self, doc):
        self._docs.append(dict(doc))

    def update_many(self, query, update):
        n = 0
        for d in self._docs:
            if _match(d, query):
                n += 1
                for k, v in (update.get("$addToSet") or {}).items():
                    arr = d.setdefault(k, [])
                    if v not in arr:
                        arr.append(v)
                for k, v in (update.get("$set") or {}).items():
                    d[k] = v
        return type("R", (), {"matched_count": n, "modified_count": n})()


class _FakeAgentDirectory:
    def __init__(self):
        self.rows = {}

    def find_one(self, filt, projection=None):
        return self.rows.get((filt.get("project"), filt.get("instance")))

    def update_one(self, filt, upd, upsert=False):
        key = (filt.get("project"), filt.get("instance"))
        row = self.rows.get(key)
        if row is None:
            if not upsert:
                return
            row = {"project": filt.get("project"), "instance": filt.get("instance")}
            self.rows[key] = row
        for k, v in (upd.get("$set") or {}).items():
            row[k] = v
        for k, v in (upd.get("$max") or {}).items():
            if row.get(k) is None or v > row[k]:
                row[k] = v


class _FakeDB:
    def __init__(self, messages, directory=None):
        self.messages = _FakeMessages(messages)
        self.agent_directory = directory or _FakeAgentDirectory()


def _msg(_id, created_at, category="info", obligation=None, priority="normal",
         to_instance="memory", to_project="junto"):
    return {
        "_id": _id,
        "to_instance": to_instance,
        "to_project": to_project,
        "from_instance": "peer",
        "from_project": "junto",
        "message": _id,
        "category": category,
        "priority": priority,
        "status": "pending",
        "obligation": obligation,
        "component": None,
        "created_at": created_at,
    }


def _setup(monkeypatch, fake_db, instance="memory", project="junto", role="agent"):
    from shared_memory.state import active_sessions
    from shared_memory.tools import messaging as m

    sid = f"_test_lanesa_{instance}_{id(fake_db)}"
    active_sessions[sid] = {"role": role, "claude_instance": instance, "project": project}
    monkeypatch.setattr(m, "get_mongo", lambda: fake_db)
    monkeypatch.setattr(m, "_is_project_admin", lambda *a, **k: False)
    return sid, m, active_sessions


# ──────────────────────────────────────────────────────────────────────────
# Per-message lane/tier
# ──────────────────────────────────────────────────────────────────────────

async def test_per_message_lane_tier_on_get_messages(monkeypatch):
    now = utc_now()
    docs = [
        _msg("open_q", now, category="question", obligation="open"),
        _msg("resp_task", now - timedelta(minutes=1), category="task", obligation="responded"),
        _msg("done_rev", now - timedelta(minutes=2), category="review", obligation="resolved"),
        _msg("fyi", now - timedelta(minutes=3), category="info"),
    ]
    sid, m, sessions = _setup(monkeypatch, _FakeDB(docs))
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, include_seen=True))
        by_id = {x["id"]: x for x in res["messages"]}
        assert (by_id["open_q"]["lane"], by_id["open_q"]["tier"]) == ("action", 0)
        assert (by_id["resp_task"]["lane"], by_id["resp_task"]["tier"]) == ("action", 1)
        assert (by_id["done_rev"]["lane"], by_id["done_rev"]["tier"]) == ("cleared", None)
        assert (by_id["fyi"]["lane"], by_id["fyi"]["tier"]) == ("fyi", None)
    finally:
        sessions.pop(sid, None)


# ──────────────────────────────────────────────────────────────────────────
# Two-tier within-page sort
# ──────────────────────────────────────────────────────────────────────────

async def test_two_tier_sort_open_then_responded_then_fyi(monkeypatch):
    """All on one page: tier dominates. open action first, responded next,
    fyi/cleared last — regardless of recency or priority across tiers."""
    now = utc_now()
    docs = [
        _msg("fyi_newest", now, category="info"),                                  # newest, but fyi
        _msg("resp_mid", now - timedelta(minutes=5), category="task", obligation="responded"),
        _msg("open_oldish", now - timedelta(minutes=10), category="question", obligation="open"),
        _msg("cleared", now - timedelta(minutes=1), category="review", obligation="resolved"),
    ]
    sid, m, sessions = _setup(monkeypatch, _FakeDB(docs))
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, include_seen=True, limit=20))
        ids = [x["id"] for x in res["messages"]]
        assert ids.index("open_oldish") < ids.index("resp_mid"), ids
        assert ids.index("resp_mid") < ids.index("fyi_newest"), ids
        assert ids.index("resp_mid") < ids.index("cleared"), ids
    finally:
        sessions.pop(sid, None)


async def test_recency_within_tier(monkeypatch):
    now = utc_now()
    docs = [
        _msg("open_old", now - timedelta(minutes=10), category="task", obligation="open"),
        _msg("open_new", now, category="task", obligation="open"),
    ]
    sid, m, sessions = _setup(monkeypatch, _FakeDB(docs))
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, include_seen=True, limit=20))
        ids = [x["id"] for x in res["messages"]]
        assert ids == ["open_new", "open_old"], ids
    finally:
        sessions.pop(sid, None)


async def test_priority_is_tiebreak_only(monkeypatch):
    """Same tier + same created → priority breaks the tie (urgent before normal).
    But priority NEVER lifts a message across tiers (covered above)."""
    now = utc_now()
    docs = [
        _msg("open_normal", now, category="task", obligation="open", priority="normal"),
        _msg("open_urgent", now, category="task", obligation="open", priority="urgent"),
    ]
    sid, m, sessions = _setup(monkeypatch, _FakeDB(docs))
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, include_seen=True, limit=20))
        ids = [x["id"] for x in res["messages"]]
        assert ids == ["open_urgent", "open_normal"], ids
    finally:
        sessions.pop(sid, None)


# ──────────────────────────────────────────────────────────────────────────
# lane_counts
# ──────────────────────────────────────────────────────────────────────────

async def test_lane_counts_mixed_inbox(monkeypatch):
    now = utc_now()
    docs = [
        _msg("a1", now, category="task", obligation="open"),
        _msg("a2", now, category="question", obligation="open"),
        _msg("r1", now, category="task", obligation="responded"),
        _msg("done", now, category="review", obligation="resolved"),   # excluded
        _msg("f1", now, category="info"),
        _msg("f2", now, category="info"),
    ]
    sid, m, sessions = _setup(monkeypatch, _FakeDB(docs))
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, include_seen=True))
        lc = res["lane_counts"]
        assert lc["pending_action_open"] == 2, lc
        assert lc["pending_action_responded"] == 1, lc
        assert lc["pending_fyi_waiting"] == 2, lc
    finally:
        sessions.pop(sid, None)


async def test_lane_counts_blocker_open(monkeypatch):
    """pending_blocker_open = UNRESOLVED blockers only (Tom statusline UX 2026-06-22).
    open + responded blockers count (still blocking); resolved excluded; non-blocker
    action categories never count toward it."""
    now = utc_now()
    docs = [
        _msg("b_open", now, category="blocker", obligation="open"),
        _msg("b_resp", now, category="blocker", obligation="responded"),  # still unresolved
        _msg("b_done", now, category="blocker", obligation="resolved"),   # excluded
        _msg("t_open", now, category="task", obligation="open"),          # not a blocker
        _msg("q_open", now, category="question", obligation="open"),      # not a blocker
    ]
    sid, m, sessions = _setup(monkeypatch, _FakeDB(docs))
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, include_seen=True))
        lc = res["lane_counts"]
        assert lc["pending_blocker_open"] == 2, lc          # b_open + b_resp
        assert lc["pending_action_open"] == 3, lc           # b_open + t_open + q_open
        assert lc["pending_action_responded"] == 1, lc      # b_resp
    finally:
        sessions.pop(sid, None)


async def test_lane_counts_watermark_asymmetry(monkeypatch):
    """A SEEN-but-open action still counts (obligations are read-independent);
    a SEEN fyi does NOT (FYIs are noise once seen); an UNSEEN fyi does.
    Uses include_seen=True so the read does not advance the watermark mid-test —
    isolating the count helper's watermark filter from the delivery side-effect
    (that side-effect is pinned separately in the next test)."""
    now = utc_now()
    seen_open = _msg("seen_open", now - timedelta(hours=2), category="task", obligation="open")
    seen_fyi = _msg("seen_fyi", now - timedelta(hours=2), category="info")
    fresh_fyi = _msg("fresh_fyi", now, category="info")
    directory = _FakeAgentDirectory()
    directory.rows[("junto", "memory")] = {
        "project": "junto", "instance": "memory",
        "messages_seen_through": now - timedelta(hours=1),  # both 'seen_*' are below
    }
    fake_db = _FakeDB([seen_open, seen_fyi, fresh_fyi], directory)
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, include_seen=True))
        lc = res["lane_counts"]
        assert lc["pending_action_open"] == 1, f"seen-but-open must still count: {lc}"
        assert lc["pending_fyi_waiting"] == 1, f"only the unseen fyi counts: {lc}"
    finally:
        sessions.pop(sid, None)


async def test_full_read_clears_fyi_but_not_action_on_badge(monkeypatch):
    """The badge tracks the read-watermark: a complete read (has_more=False)
    advances the watermark to the newest delivered message, so on THIS response
    pending_fyi_waiting drops to 0 (you've now seen them) while pending_action_open
    persists (actions nag until explicitly resolved, independent of read state).
    This is the intended badge UX and the reason lane_counts is computed AFTER the
    watermark advance."""
    now = utc_now()
    docs = [
        _msg("open_action", now - timedelta(minutes=5), category="blocker", obligation="open"),
        _msg("fyi", now, category="info"),
    ]
    fake_db = _FakeDB(docs)  # no prior watermark → everything unseen
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, limit=20))
        assert res["has_more"] is False
        lc = res["lane_counts"]
        assert lc["pending_fyi_waiting"] == 0, f"fyi seen by this read → cleared: {lc}"
        assert lc["pending_action_open"] == 1, f"action persists regardless of read: {lc}"
    finally:
        sessions.pop(sid, None)


# ──────────────────────────────────────────────────────────────────────────
# Inbox's co-sign blocking condition: selection stays recency-primary
# ──────────────────────────────────────────────────────────────────────────

async def test_selection_stays_recency_primary(monkeypatch):
    """The tier sort is WITHIN-PAGE only. An OLD open action must NOT be pulled
    onto page-1 ahead of newer fyi — selection is created_at-primary, so page-1
    is the newest `limit` by created_at. (If the tier key had migrated to the DB
    selection sort, old_open would jump to page-1 and the created_at cursor would
    go incoherent — the design:inbox-surfacing-v0 bug inbox flagged.) lane_counts
    still sees the off-page action because it counts the FULL inbox."""
    now = utc_now()
    docs = [_msg(f"fyi{i}", now - timedelta(minutes=i), category="info") for i in range(20)]
    old_open = _msg("old_open", now - timedelta(days=1), category="blocker", obligation="open")
    docs.append(old_open)
    sid, m, sessions = _setup(monkeypatch, _FakeDB(docs))
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, include_seen=True, limit=20))
        page_ids = [x["id"] for x in res["messages"]]
        assert "old_open" not in page_ids, "old action stranded onto page-1 → selection not recency-primary"
        assert res["has_more"] is True
        assert res["lane_counts"]["pending_action_open"] == 1, "off-page action must still be counted"
    finally:
        sessions.pop(sid, None)


# ──────────────────────────────────────────────────────────────────────────
# read_inbox (push path) parity + nimbus degenerate
# ──────────────────────────────────────────────────────────────────────────

async def test_read_inbox_emits_lane_and_counts(monkeypatch):
    now = utc_now()
    docs = [
        _msg("a1", now, category="task", obligation="open"),
        _msg("f1", now - timedelta(minutes=1), category="info"),
    ]
    fake_db = _FakeDB(docs)
    from shared_memory.tools import messaging as m
    monkeypatch.setattr(m, "get_mongo", lambda: fake_db)
    monkeypatch.setattr(m, "_check_inbox_authz", lambda *a, **k: (True, ""))
    monkeypatch.setattr(m.push_control, "should_deliver_via_push_filter", lambda *a, **k: True)
    res = json.loads(await m.read_inbox("junto", "memory"))
    by_id = {x["id"]: x for x in res["messages"]}
    assert (by_id["a1"]["lane"], by_id["a1"]["tier"]) == ("action", 0)
    assert by_id["f1"]["lane"] == "fyi"
    assert res["lane_counts"]["pending_action_open"] == 1
    assert res["lane_counts"]["pending_fyi_waiting"] == 1  # no watermark on push path → all pending info


async def test_nimbus_degenerate_direct_only(monkeypatch):
    """All direct sends, no component, mixed obligations — sort + counts work
    identically (component plays no role)."""
    now = utc_now()
    docs = [
        _msg("open", now - timedelta(minutes=2), category="task", obligation="open"),
        _msg("fyi", now, category="info"),
    ]
    sid, m, sessions = _setup(monkeypatch, _FakeDB(docs))
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, include_seen=True, limit=20))
        ids = [x["id"] for x in res["messages"]]
        assert ids == ["open", "fyi"], f"open action leads despite fyi being newer: {ids}"
        assert res["lane_counts"]["pending_action_open"] == 1
    finally:
        sessions.pop(sid, None)
