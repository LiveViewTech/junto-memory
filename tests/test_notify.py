"""Human notifications (interface:human-notify-v0): registry, queue, claim/report, delegation routing."""

import os
import uuid
from datetime import timedelta

import pytest

from shared_memory import delegation as di
from shared_memory import notify as nt
from shared_memory.helpers import utc_now


@pytest.fixture
def db():
    """Throwaway database on the TEST mongo (JUNTO_TEST_MONGO_URL), never production."""
    url = os.environ.get("JUNTO_TEST_MONGO_URL")
    if not url:
        pytest.skip("JUNTO_TEST_MONGO_URL not set (use a throwaway mongo, not production)")
    from pymongo import MongoClient
    client = MongoClient(url, serverSelectionTimeoutMS=3000)
    try:
        client.admin.command("ping")
    except Exception:
        pytest.skip("test mongo not reachable")
    name = f"notify_test_{uuid.uuid4().hex[:8]}"
    nt._indexed = False
    try:
        yield client[name]
    finally:
        client.drop_database(name)
        client.close()


SENDER = {"agent": "et-engine", "project": "emailtriage", "kind": "agent"}


def _reg(db, **kw):
    args = dict(name="tom", actor="tomclaude", actor_role="agent", channels={"gmail": "tom@example.com"})
    args.update(kw)
    return nt.register_human(db, **args)


def test_register_and_list(db):
    out = _reg(db, preferred="gmail", quiet_hours="22:00-07:00", timezone="America/Chicago")
    assert out["status"] == "registered"
    h = nt.list_humans(db)[0]
    assert h["name"] == "tom" and h["channels"] == ["gmail"] and h["verified"] == {}


def test_only_creator_or_human_can_change_channels(db):
    _reg(db)
    out = nt.register_human(db, "tom", "someone-else", "agent", {"gmail": "attacker@example.com"})
    assert "error" in out
    assert db.humans.find_one({"_id": "tom"})["channels"]["gmail"] == "tom@example.com"
    ok = nt.register_human(db, "tom", "tom-in-web", "user", {"gmail": "tom2@example.com"})
    assert ok["status"] == "registered"


def test_send_queues_redacts_and_warns_unverified(db):
    _reg(db)
    out = nt.enqueue(db, "human:tom", "Daily issues", "3 issues. MONGO_PASSWORD=T0mTest99", SENDER)
    assert out["status"] == "queued" and out["channel"] == "gmail"
    assert "not verified" in out["warning"] and out["redacted"] == {"assigned_secret": 1}
    row = db.notifications.find_one({"_id": out["notification_id"]})
    assert "T0mTest99" not in row["body"] and row["status"] == "queued"


def test_unknown_human_and_bad_channel(db):
    assert "no registered human" in nt.enqueue(db, "human:nobody", "s", "b", SENDER)["error"]
    _reg(db)
    assert "has no channel" in nt.enqueue(db, "tom", "s", "b", SENDER, channel="slack")["error"]


def test_dedup_and_daily_cap(db):
    _reg(db)
    first = nt.enqueue(db, "tom", "same", "b", SENDER)
    assert nt.enqueue(db, "tom", "same", "b2", SENDER)["status"] == "duplicate"
    for i in range(nt.DAILY_CAP - 1):
        assert nt.enqueue(db, "tom", f"s{i}", "b", SENDER)["status"] == "queued"
    assert "daily cap" in nt.enqueue(db, "tom", "one too many", "b", SENDER)["error"]
    assert first["status"] == "queued"


def test_truncation_keeps_full_body_for_status(db):
    _reg(db)
    big = "x" * (nt.BODY_CAP + 500)
    out = nt.enqueue(db, "tom", "big", big, SENDER)
    assert out["truncated"] is True
    row = db.notifications.find_one({"_id": out["notification_id"]})
    assert len(row["body"]) < len(big) and "truncated" in row["body"]
    assert nt.status(db, notification_id=out["notification_id"])["notification"]["body"] == big


def test_claim_report_sent_and_test_verifies(db):
    _reg(db)
    t = nt.enqueue(db, "tom", "junto notify test", "hi", SENDER, urgency="urgent", test=True)
    claimed = nt.claim(db, "tom", "laptop")
    assert [n["notification_id"] for n in claimed["notifications"]] == [t["notification_id"]]
    assert claimed["notifications"][0]["destination"] == "tom@example.com"
    assert nt.claim(db, "tom", "laptop")["notifications"] == []          # leased
    assert nt.report(db, t["notification_id"], True, provider_id="gm1")["status"] == "sent"
    assert db.humans.find_one({"_id": "tom"})["verified"]["gmail"]
    assert db.humans.find_one({"_id": "tom"})["runner"]["host"] == "laptop"


def test_failure_requeues_then_goes_final(db):
    _reg(db)
    n = nt.enqueue(db, "tom", "s", "b", SENDER)
    for attempt in range(1, nt.MAX_ATTEMPTS + 1):
        assert nt.claim(db, "tom", "h")["notifications"]
        res = nt.report(db, n["notification_id"], False, error="connector down")
        assert res["final"] is (attempt == nt.MAX_ATTEMPTS)
    assert db.notifications.find_one({"_id": n["notification_id"]})["status"] == "failed"


def test_expired_lease_is_reclaimable(db):
    _reg(db)
    n = nt.enqueue(db, "tom", "s", "b", SENDER)
    nt.claim(db, "tom", "h1")
    db.notifications.update_one({"_id": n["notification_id"]},
                                {"$set": {"lease_until": utc_now() - timedelta(minutes=1)}})
    assert nt.claim(db, "tom", "h2")["notifications"][0]["notification_id"] == n["notification_id"]


def test_quiet_hours_hold_normal_but_not_urgent(db):
    _reg(db, quiet_hours="00:00-23:59", timezone="UTC")
    nt.enqueue(db, "tom", "normal one", "b", SENDER)
    u = nt.enqueue(db, "tom", "urgent one", "b", SENDER, urgency="urgent")
    got = nt.claim(db, "tom", "h")
    assert got["quiet_hours_active"] is True
    assert [n["notification_id"] for n in got["notifications"]] == [u["notification_id"]]


# --- delegation integration ------------------------------------------------------
def test_route_validation_accepts_human_target():
    scope = {"source": "inline"}
    schema = {"type": "object", "properties": {"severity": {"type": "string"}}}
    base = {"name": "t", "instructions": "x", "output_schema": schema, "scope": "s"}
    ok = di.validate_task_fields({**base, "routes": [{"if": {"field": "severity", "op": "eq", "value": "high"},
                                                      "then": "notify", "to": "human:tom", "urgency": "urgent"}]},
                                 scope)
    assert not [e for e in ok if "routes" in e], ok
    bad = di.validate_task_fields({**base, "routes": [{"then": "notify", "to": "human:"}]}, scope)
    assert any("human:<name>" in e for e in bad)
    sched = di.validate_task_fields({**base, "schedule": {"every": "1d", "digest_to": "human:tom"}}, scope)
    assert not [e for e in sched if "digest_to" in e], sched


@pytest.mark.asyncio
async def test_send_notices_enqueues_for_humans_and_tells_owner_on_failure(db):
    _reg(db)
    n = {"to": ["human:tom", "human:ghost"], "owner": "emailtriage/et-engine", "subject": "[daily] 3 issues",
         "body": "- a\n- b\n- c", "kind": "delegated_digest", "category": "info"}
    await di.send_notices(db, [n])
    rows = list(db.notifications.find({"to": "tom"}))
    assert len(rows) == 1 and rows[0]["sender"]["agent"] == "et-engine"
    owner_msgs = list(db.messages.find({"to_instance": "et-engine", "to_project": "emailtriage"}))
    assert any("human:ghost" in (m.get("subject") or "") for m in owner_msgs)
