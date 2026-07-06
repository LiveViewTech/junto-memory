"""Read-state regression (design:message-read-watermark-v0 → superseded by
per-message read, design:server-authoritative-delivery-v0 §E / build-plan task 2).

ORIGINAL (2026-06-10): a single per-recipient messages_seen_through watermark that
AUTO-ADVANCED on full reads. That conflated "I glanced at a new message" with
"every older message is now read".

NOW (task 2): read state is PER-MESSAGE — read_by excludes the agent. A body-
returning owner read marks ONLY the returned messages read (not the older tail);
ack marks read too. The watermark is DEMOTED to a coarse floor (pre-task-2
history) and no longer auto-advances. These tests pin the new contract:

  - a top owner read defaults to UNREAD-only (read_by ne me [+ watermark floor])
  - a full read MARKS the returned messages read (per-message), does NOT advance
    the watermark; a truncated page marks ONLY the page, the tail stays unread
  - include_seen=True is a full-window catch-up that neither filters nor marks
  - cursor pagination / for_instance peeks bypass entirely

The inbox:// resource path (push delivery, control UI) is a SEPARATE function
(read_inbox), read-INERT: it never filters by read_by and never marks read.
"""

import json
from datetime import timedelta

from shared_memory.helpers import utc_now


# ── Minimal Mongo query matcher (only the shapes get_messages produces) ──
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
                    # array-aware: {read_by:{$ne:x}} excludes a doc whose array
                    # field contains x, not just scalar equality
                    if isinstance(val, list):
                        if operand in val:
                            return False
                    elif val == operand:
                        return False
                elif op == "$in" and val not in operand:
                    return False
                elif op == "$exists":
                    present = key in doc
                    if present != operand:
                        return False
        else:
            if doc.get(key) != cond:
                return False
    return True


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, spec):
        # Apply spec right-to-left so the leftmost key is primary.
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

    def find_one(self, query, projection=None):
        for d in self._docs:
            if _match(d, query):
                return d
        return None

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

    def update_one(self, query, update, upsert=False):
        for d in self._docs:
            if _match(d, query):
                if isinstance(update, dict):  # ignore pipeline (list) updates
                    for k, v in (update.get("$set") or {}).items():
                        d[k] = v
                    for k, v in (update.get("$addToSet") or {}).items():
                        arr = d.setdefault(k, [])
                        if v not in arr:
                            arr.append(v)
                return type("R", (), {"matched_count": 1, "modified_count": 1})()
        return type("R", (), {"matched_count": 0, "modified_count": 0})()


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


def _msg(_id, created_at, to_instance="memory", to_project="junto"):
    return {
        "_id": _id,
        "to_instance": to_instance,
        "to_project": to_project,
        "from_instance": "peer",
        "from_project": "junto",
        "message": _id,
        "category": "info",
        "priority": "normal",
        "status": "pending",
        "created_at": created_at,
    }


def _setup(monkeypatch, fake_db, instance="memory", project="junto", role="agent"):
    from shared_memory.state import active_sessions
    from shared_memory.tools import messaging as m

    sid = f"_test_watermark_{instance}_{id(fake_db)}"
    active_sessions[sid] = {"role": role, "claude_instance": instance, "project": project}
    monkeypatch.setattr(m, "get_mongo", lambda: fake_db)
    monkeypatch.setattr(m, "_is_project_admin", lambda *a, **k: False)
    return sid, m, active_sessions


async def test_default_read_filters_out_seen_messages(monkeypatch):
    now = utc_now()
    old = _msg("old", now - timedelta(hours=2))
    new = _msg("new", now)
    directory = _FakeAgentDirectory()
    directory.rows[("junto", "memory")] = {
        "project": "junto", "instance": "memory",
        "messages_seen_through": now - timedelta(hours=1),
    }
    fake_db = _FakeDB([old, new], directory)
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid))
        ids = [x["id"] for x in res["messages"]]
        assert ids == ["new"], f"watermark should hide 'old'; got {ids}"
    finally:
        sessions.pop(sid, None)


async def test_full_read_marks_returned_messages_read(monkeypatch):
    """Per-message read (task 2): a full owner read marks the RETURNED messages
    read_by the agent (replacing the watermark auto-advance, now demoted). A
    second read excludes them; the watermark is NOT advanced by a scan."""
    now = utc_now()
    m1 = _msg("m1", now - timedelta(minutes=10))
    m2 = _msg("m2", now)
    fake_db = _FakeDB([m1, m2])  # no prior watermark
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, limit=20))
        assert res["has_more"] is False
        assert {x["id"] for x in res["messages"]} == {"m1", "m2"}
        # both returned docs are now marked read by the agent (per-message)
        for d in fake_db.messages._docs:
            assert "memory" in d.get("read_by", []), f"{d['_id']} not marked read"
        # watermark is DEMOTED — a scan does not advance it
        assert fake_db.agent_directory.rows.get(("junto", "memory")) is None
        # a second read returns nothing — everything is read
        res2 = json.loads(await m.memory_get_messages(session_id=sid, limit=20))
        assert res2["messages"] == [], "read messages must not re-surface"
    finally:
        sessions.pop(sid, None)


async def test_recency_primary_sort_does_not_strand_urgent(monkeypatch):
    """design:inbox-surfacing-v0: priority is a STRING, so DB-sorting on it
    (low<normal<urgent) before .limit() stranded urgent behind a large normal
    backlog. Recency-primary must surface the newest messages regardless of
    priority — a recent urgent is never buried; a stale urgent pages back.
    Fails on the old [("priority",1),("created_at",-1)] sort."""
    now = utc_now()
    docs = [_msg(f"normal{i}", now - timedelta(minutes=10 - i)) for i in range(5)]
    u_new = _msg("urgent_new", now)  # newest of all
    u_new["priority"] = "urgent"
    docs.append(u_new)
    u_old = _msg("urgent_old", now - timedelta(hours=3))  # stale urgent
    u_old["priority"] = "urgent"
    docs.append(u_old)

    fake_db = _FakeDB(docs)
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, limit=3))
        ids = {x["id"] for x in res["messages"]}
        # newest 3 by created_at = urgent_new(now), normal4(-6m), normal3(-7m)
        assert "urgent_new" in ids, f"recent urgent stranded behind normal: {ids}"
        assert "urgent_old" not in ids, f"stale urgent should page back: {ids}"
        assert res["has_more"] is True
    finally:
        sessions.pop(sid, None)


async def test_truncated_read_marks_only_returned_tail_stays_unread(monkeypatch):
    """Per-message marking marks ONLY the page handed over — the older unreturned
    tail stays unread (the old has_more guard is obsolete). m2 (newest) is
    returned + marked; m1 stays unread and surfaces on the next read."""
    now = utc_now()
    m1 = _msg("m1", now - timedelta(minutes=10))
    m2 = _msg("m2", now)
    fake_db = _FakeDB([m1, m2])
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, limit=1))
        assert res["has_more"] is True
        assert [x["id"] for x in res["messages"]] == ["m2"]  # newest-first selection
        by_id = {d["_id"]: d for d in fake_db.messages._docs}
        assert "memory" in by_id["m2"].get("read_by", [])
        assert "memory" not in by_id["m1"].get("read_by", []), "tail must stay unread"
        # the unread tail surfaces on the next read
        res2 = json.loads(await m.memory_get_messages(session_id=sid, limit=1))
        assert [x["id"] for x in res2["messages"]] == ["m1"]
    finally:
        sessions.pop(sid, None)


async def test_cursor_read_bypasses_and_does_not_advance(monkeypatch):
    """CONTRACT INVARIANT (inbox sign-off, msg_ff971862026c): get_messages with
    a cursor present never watermark-filters AND never advances. The junto-inbox
    plugin's page-2+ pagination (readInboxAndForward) relies on this — if it
    regresses, the plugin silently drops the page-2+ tail of >20-msg backlogs."""
    now = utc_now()
    old = _msg("old", now - timedelta(hours=2))
    directory = _FakeAgentDirectory()
    wm = now  # watermark AHEAD of everything — would hide 'old' if applied
    directory.rows[("junto", "memory")] = {
        "project": "junto", "instance": "memory", "messages_seen_through": wm,
    }
    fake_db = _FakeDB([old], directory)
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        cursor = (now - timedelta(hours=1)).isoformat()
        res = json.loads(
            await m.memory_get_messages(session_id=sid, cursor=cursor, limit=20)
        )
        assert [x["id"] for x in res["messages"]] == ["old"], (
            "cursor read must ignore the watermark filter"
        )
        assert directory.rows[("junto", "memory")]["messages_seen_through"] == wm, (
            "cursor read must not advance the watermark"
        )
    finally:
        sessions.pop(sid, None)


async def test_inbox_resource_never_filters_never_advances(monkeypatch):
    """CONTRACT INVARIANT (inbox sign-off, msg_ff971862026c): inbox:// resource
    reads (read_inbox — the plugin's page-1 push path and control's UI) are
    never watermark-filtered and never advance the watermark."""
    from shared_memory.tools import messaging as m

    now = utc_now()
    old = _msg("old", now - timedelta(hours=2))
    new = _msg("new", now)
    directory = _FakeAgentDirectory()
    wm = now + timedelta(minutes=1)  # ahead of BOTH messages
    directory.rows[("junto", "memory")] = {
        "project": "junto", "instance": "memory", "messages_seen_through": wm,
    }
    fake_db = _FakeDB([old, new], directory)
    monkeypatch.setattr(m, "get_mongo", lambda: fake_db)
    monkeypatch.setattr(m, "_check_inbox_authz", lambda p, a: (True, ""))
    monkeypatch.setattr(
        m.push_control, "should_deliver_via_push_filter", lambda db, p, a: True
    )
    res = json.loads(await m.read_inbox("junto", "memory"))
    assert {x["id"] for x in res["messages"]} == {"old", "new"}, (
        "resource read must return the full pending window regardless of watermark"
    )
    assert directory.rows[("junto", "memory")]["messages_seen_through"] == wm, (
        "resource read must not advance the watermark"
    )


async def test_ack_marks_message_read(monkeypatch):
    """An ack (status=received) marks the message read by the acking agent — the
    other half of body-pull-marks-read (build-plan task 2). After ack it drops
    from the default unread view."""
    now = utc_now()
    q = _msg("q1", now)
    q["category"] = "question"
    q["obligation"] = "open"
    fake_db = _FakeDB([q])
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        await m.memory_update_message_status(session_id=sid, message_id="q1", status="received")
        doc = fake_db.messages._docs[0]
        assert "memory" in doc.get("read_by", []), "ack must mark read_by the acker"
        res = json.loads(await m.memory_get_messages(session_id=sid, limit=20))
        assert [x["id"] for x in res["messages"]] == [], "acked message must not re-surface"
    finally:
        sessions.pop(sid, None)


async def test_fetch_by_id_marks_read(monkeypatch):
    """Fetching your OWN message body by id is a canonical body-pull → marks it
    read (build-plan task 2)."""
    now = utc_now()
    fake_db = _FakeDB([_msg("m1", now)])
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        await m.memory_get_messages(session_id=sid, message_id="m1")
        assert "memory" in fake_db.messages._docs[0].get("read_by", [])
    finally:
        sessions.pop(sid, None)


async def test_read_inbox_is_read_inert_does_not_zero_m(monkeypatch):
    """The inbox:// resource read is READ-INERT: it never writes read_by, so the
    fyi M-count is NOT zeroed by surfacing the badge (the old M-zeroing bug). The
    info message stays unread + counted after a resource read."""
    from shared_memory.tools import messaging as m

    now = utc_now()
    info = _msg("i1", now)  # category=info by default
    fake_db = _FakeDB([info])
    monkeypatch.setattr(m, "get_mongo", lambda: fake_db)
    monkeypatch.setattr(m, "_check_inbox_authz", lambda p, a: (True, ""))
    monkeypatch.setattr(
        m.push_control, "should_deliver_via_push_filter", lambda db, p, a: True
    )
    res = json.loads(await m.read_inbox("junto", "memory"))
    assert res["lane_counts"]["pending_fyi_waiting"] == 1, "M must survive a resource read"
    assert "memory" not in fake_db.messages._docs[0].get("read_by", []), (
        "resource read must NOT mark read"
    )


async def test_headers_only_is_inert_and_omits_body(monkeypatch):
    """headers_only (task 3) is a read-INERT triage scan: returns metadata with
    NO body and does NOT mark read, so the message still surfaces (with body) on
    a subsequent normal read."""
    now = utc_now()
    d = _msg("m1", now)
    d["subject"] = "the subject"
    fake_db = _FakeDB([d])
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, headers_only=True))
        row = res["messages"][0]
        assert row["id"] == "m1"
        assert row["subject"] == "the subject"   # metadata present
        assert row["message"] is None            # body omitted
        assert "memory" not in fake_db.messages._docs[0].get("read_by", [])  # inert
        # a normal read still surfaces it (with body) and marks it read
        res2 = json.loads(await m.memory_get_messages(session_id=sid))
        assert res2["messages"][0]["message"] == "m1"
        assert "memory" in fake_db.messages._docs[0].get("read_by", [])
    finally:
        sessions.pop(sid, None)


async def test_created_after_before_bounds(monkeypatch):
    """Explicit created_at bounds (task 3). include_seen=True isolates the bound
    from the unread filter."""
    now = utc_now()
    docs = [
        _msg("old", now - timedelta(hours=3)),
        _msg("mid", now - timedelta(hours=1)),
        _msg("new", now),
    ]
    fake_db = _FakeDB(docs)
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        after = (now - timedelta(hours=2)).isoformat()
        res = json.loads(await m.memory_get_messages(
            session_id=sid, include_seen=True, created_after=after))
        assert {x["id"] for x in res["messages"]} == {"mid", "new"}

        before = (now - timedelta(minutes=30)).isoformat()
        res2 = json.loads(await m.memory_get_messages(
            session_id=sid, include_seen=True, created_after=after, created_before=before))
        assert {x["id"] for x in res2["messages"]} == {"mid"}
    finally:
        sessions.pop(sid, None)


async def test_fyi_aging_signal_in_counts(monkeypatch):
    """FYI aging signal (guidance, not force): lane_counts reports the oldest
    unread FYI's age + the info TTL, so an agent can be nudged to drain FYIs
    before they age out at 48h. Nothing auto-expires on it."""
    now = utc_now()
    old = _msg("fyi_old", now - timedelta(hours=30))
    new = _msg("fyi_new", now - timedelta(hours=5))
    fake_db = _FakeDB([old, new])
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, include_seen=True))
        lc = res["lane_counts"]
        assert lc["pending_fyi_waiting"] == 2
        assert lc["fyi_ttl_hours"] == 48
        assert 29.0 <= lc["pending_fyi_oldest_age_hours"] <= 31.0, lc
    finally:
        sessions.pop(sid, None)


async def test_fyi_aging_none_when_no_fyi(monkeypatch):
    now = utc_now()
    q = _msg("q1", now)
    q["category"] = "question"
    q["obligation"] = "open"
    fake_db = _FakeDB([q])
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, include_seen=True))
        lc = res["lane_counts"]
        assert lc["pending_fyi_waiting"] == 0
        assert lc["pending_fyi_oldest_age_hours"] is None
    finally:
        sessions.pop(sid, None)


async def test_include_seen_bypasses_and_does_not_advance(monkeypatch):
    now = utc_now()
    old = _msg("old", now - timedelta(hours=2))
    new = _msg("new", now)
    directory = _FakeAgentDirectory()
    wm = now - timedelta(hours=1)
    directory.rows[("junto", "memory")] = {
        "project": "junto", "instance": "memory", "messages_seen_through": wm,
    }
    fake_db = _FakeDB([old, new], directory)
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        res = json.loads(
            await m.memory_get_messages(session_id=sid, include_seen=True, limit=20)
        )
        assert {x["id"] for x in res["messages"]} == {"old", "new"}, "catch-up shows all"
        assert fake_db.agent_directory.rows[("junto", "memory")]["messages_seen_through"] == wm, (
            "include_seen must not advance the watermark"
        )
    finally:
        sessions.pop(sid, None)
