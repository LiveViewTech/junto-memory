"""Server-authoritative content-push — the §ANNOUNCE-PUSH transport.

design:server-authoritative-delivery-v0 v0.5.1, ratified into
contract:message-lanes-v0 §E. Pins the SERVER half of the delivery flip:

  - _announce_mode: the inject/header/badge-only classification on top of
    classify_lane. Since push-all-info-v0 (2026-06-24) info/fyi pushes as a HEADER
    too; ONLY a resolved/cleared action stays badge-only (None).
  - _build_announce_packet: the FROZEN wire packet (§E3) — field set, inline body
    iff inject, created_at as an ISO string, and JSON-serializability (the dict
    rides a raw JSON-RPC notification on the write stream).
  - _content_push: the custom method notifications/junto/announce goes out via the
    low-level send_message escape hatch (send_notification would reject a non-union
    method) — learning_5dcf4824df37700f.
  - _notify_inbox: content-push is ADDITIVE — resource-updated still fires (pre-
    cutover plugin), the announce push rides alongside (post-cutover plugin), and
    packet=None (wake-all / cleared-action) keeps the resource-updated-only path.
"""

import json
import os
import sys

from datetime import timedelta

sys.path.insert(0, os.path.dirname(__file__))  # sibling test-module reuse (no conftest)

from shared_memory.helpers import utc_now
from shared_memory.tools import messaging as m


def _doc(_id="msg_x", category="info", obligation=None, priority="normal",
         require_human=False, is_system_notice=False, message="hello",
         in_response_to=None, chain_depth=0, created_at=None):
    return {
        "_id": _id,
        "to_instance": "memory",
        "to_project": "junto",
        "from_instance": "peer",
        "from_project": "junto",
        "message": message,
        "category": category,
        "priority": priority,
        "obligation": obligation,
        "require_human": require_human,
        "is_system_notice": is_system_notice,
        "in_response_to": in_response_to,
        "chain_depth": chain_depth,
        "created_at": created_at or utc_now(),
    }


# ── _announce_mode ──────────────────────────────────────────────────────────

def test_mode_inject_set():
    # blocker | urgent | require_human | system_notice → inject
    assert m._announce_mode("blocker", "normal", False, False, "open") == "inject"
    assert m._announce_mode("question", "urgent", False, False, "open") == "inject"
    assert m._announce_mode("task", "normal", True, False, "open") == "inject"
    assert m._announce_mode("review", "normal", False, True, "open") == "inject"


def test_mode_header_for_plain_action():
    for cat in ("task", "question", "review", "contract"):
        assert m._announce_mode(cat, "normal", False, False, "open") == "header"
        assert m._announce_mode(cat, "low", False, False, "open") == "header"


def test_mode_header_for_info():
    # push-all-info-v0: a plain info now pushes as a metadata-only header
    assert m._announce_mode("info", "normal", False, False, None) == "header"
    assert m._announce_mode("info", "low", False, False, None) == "header"


def test_mode_info_escalates_to_inject_when_must_read_now():
    # urgent / require_human / system_notice info escalates to a full-body inject
    assert m._announce_mode("info", "urgent", False, False, None) == "inject"
    assert m._announce_mode("info", "normal", True, False, None) == "inject"   # require_human
    assert m._announce_mode("info", "normal", False, True, None) == "inject"   # system_notice


def test_mode_none_only_for_cleared():
    # the ONLY badge-only case left: a RESOLVED action (lane "cleared")
    assert m._announce_mode("task", "normal", False, False, "resolved") is None
    assert m._announce_mode("question", "normal", False, False, "resolved") is None


# ── _build_announce_packet ──────────────────────────────────────────────────

FROZEN_FIELDS = {
    "mode", "from_agent", "from_project", "category", "priority", "msg_id",
    "chain_depth", "in_response_to", "obligation_state", "subject",
    "require_human", "is_system_notice", "created_at",
}


def test_packet_header_for_info():
    # push-all-info-v0: info builds a header packet (subject+from, NO body)
    pkt = m._build_announce_packet(_doc(category="info"))
    assert pkt is not None
    assert pkt["mode"] == "header"
    assert "body" not in pkt
    assert set(pkt.keys()) == FROZEN_FIELDS


def test_packet_none_for_cleared_action():
    # a resolved action is the only badge-only (None) case now
    assert m._build_announce_packet(_doc(category="task", obligation="resolved")) is None


def test_packet_header_has_frozen_fields_no_body():
    pkt = m._build_announce_packet(_doc(_id="m1", category="question", obligation="open"))
    assert pkt is not None
    assert set(pkt.keys()) == FROZEN_FIELDS  # header carries NO body
    assert pkt["mode"] == "header"
    assert pkt["msg_id"] == "m1"
    assert pkt["from_agent"] == "peer"
    assert pkt["obligation_state"] == "open"
    assert pkt["subject"] is None  # subject field present, None when sender omits it


def test_packet_inject_inlines_body():
    pkt = m._build_announce_packet(
        _doc(category="blocker", obligation="open", message="STOP the build")
    )
    assert pkt["mode"] == "inject"
    assert pkt["body"] == "STOP the build"
    assert set(pkt.keys()) == FROZEN_FIELDS | {"body"}


def test_packet_created_at_is_iso_string_and_json_safe():
    pkt = m._build_announce_packet(_doc(category="task", obligation="open"))
    assert isinstance(pkt["created_at"], str)  # not a datetime
    # the dict must survive raw JSON-RPC serialization
    round_tripped = json.loads(json.dumps(pkt))
    assert round_tripped["msg_id"] == pkt["msg_id"]


def test_packet_subject_passthrough_when_present():
    doc = _doc(category="task", obligation="open")
    doc["subject"] = "ship it"
    assert m._build_announce_packet(doc)["subject"] == "ship it"


# ── _content_push + _notify_inbox ───────────────────────────────────────────

class _FakeSession:
    def __init__(self, fail_on=None):
        self.resource_updates = []
        self.sent = []
        self._fail_on = fail_on  # "resource" | "push" | None

    async def send_resource_updated(self, url):
        if self._fail_on == "resource":
            raise RuntimeError("dead transport")
        self.resource_updates.append(url)

    async def send_message(self, session_message):
        if self._fail_on == "push":
            raise RuntimeError("dead transport")
        self.sent.append(session_message)


async def test_content_push_uses_custom_method_and_packet():
    sess = _FakeSession()
    pkt = {"mode": "header", "msg_id": "m9"}
    await m._content_push(sess, pkt)
    assert len(sess.sent) == 1
    notif = sess.sent[0].message.root
    assert notif.method == m.ANNOUNCE_METHOD == "notifications/junto/announce"
    assert notif.params == pkt


async def test_notify_inbox_additive_push(monkeypatch):
    sess = _FakeSession()
    uri = m.inbox_uri("junto", "memory")
    monkeypatch.setitem(m.inbox_subscriptions, uri, {sess})
    pkt = m._build_announce_packet(_doc(category="question", obligation="open"))

    await m._notify_inbox("junto", "memory", pkt)

    # BOTH fire: resource-updated (pre-cutover plugin) + announce push (post-cutover)
    assert len(sess.resource_updates) == 1
    assert len(sess.sent) == 1
    assert sess.sent[0].message.root.method == m.ANNOUNCE_METHOD


async def test_notify_inbox_none_packet_is_resource_only(monkeypatch):
    sess = _FakeSession()
    uri = m.inbox_uri("junto", "memory")
    monkeypatch.setitem(m.inbox_subscriptions, uri, {sess})

    await m._notify_inbox("junto", "memory", None)

    assert len(sess.resource_updates) == 1
    assert len(sess.sent) == 0  # no content-push for the wake-all / badge-only path


async def test_notify_inbox_prunes_dead_session(monkeypatch):
    dead = _FakeSession(fail_on="push")
    uri = m.inbox_uri("junto", "memory")
    monkeypatch.setitem(m.inbox_subscriptions, uri, {dead})
    pkt = m._build_announce_packet(_doc(category="task", obligation="open"))

    await m._notify_inbox("junto", "memory", pkt)

    # a content-push failure marks the session dead and prunes the (now empty) bucket
    assert uri not in m.inbox_subscriptions


# ── SUBJECT surfaces on the pull/read path (build-plan task 1) ───────────────
# Reuses the working lanes-A get_messages harness instead of rebuilding the
# op_log/push_control send scaffolding.
from test_lanes_a_wire import _FakeDB, _msg, _setup  # noqa: E402


async def test_get_messages_surfaces_subject(monkeypatch):
    now = utc_now()
    doc = _msg("m_sub", now, category="task", obligation="open")
    doc["subject"] = "ship the thing"
    plain = _msg("m_plain", now - timedelta(minutes=1), category="info")  # no subject
    sid, mod, sessions = _setup(monkeypatch, _FakeDB([doc, plain]))
    try:
        res = json.loads(await mod.memory_get_messages(session_id=sid, include_seen=True))
        by_id = {x["id"]: x for x in res["messages"]}
        assert by_id["m_sub"]["subject"] == "ship the thing"
        assert by_id["m_plain"]["subject"] is None  # absent field serializes as None
    finally:
        sessions.pop(sid, None)
