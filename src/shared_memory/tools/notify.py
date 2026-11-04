"""memory_notify: send a message to a PERSON (interface:human-notify-v0)."""

import json

from mcp.server.fastmcp import Context

from shared_memory import notify as nt
from shared_memory.app import mcp
from shared_memory.clients import get_mongo
from shared_memory.helpers import require_session
from shared_memory.state import active_sessions

ACTIONS = ("send", "register", "humans", "status", "test", "claim", "report")


@mcp.tool()
async def memory_notify(
    session_id: str,
    action: str = "send",
    to: str = None,
    subject: str = None,
    body: str = None,
    urgency: str = "normal",
    channel: str = "auto",
    name: str = None,
    gmail: str = None,
    slack: str = None,
    email: str = None,
    preferred: str = None,
    quiet_hours: str = None,
    timezone: str = None,
    notification_id: str = None,
    runner_host: str = None,
    ok: bool = None,
    provider_id: str = None,
    error: str = None,
    limit: int = 5,
    ctx: Context = None,
) -> str:
    """
    Send a message to a person (not an agent) over their own Slack or Gmail.

    Junto queues it; the person's notify runner (on their machine, logged into
    their Claude account) delivers it through their approved Claude connector.
    YOU write the content: subject + body are sent as-is (secrets redacted,
    body capped at 3,000 chars). Use this when a human must see something and
    no agent will be reading an inbox, e.g. a scheduled local-model job's
    daily issues list. Scheduled tasks can also route to a person directly:
    route "to": "human:<name>" (notify), or schedule "digest_to": "human:<name>".

    Actions:
      send (default)  to="human:tom" (or "tom"), subject, body, urgency normal|urgent,
                      channel auto|gmail|slack. Returns notification_id. Max 20/person/day;
                      the same subject from you within 1h is not re-sent.
      register        name, and gmail= / slack= / email= destinations; preferred,
                      quiet_hours "22:00-07:00", timezone. Each person registers
                      their own channels; changing someone else's needs a human/admin.
      humans          list registered people + which channels are verified.
      test            to= a person: queue a test message; when their runner sends it,
                      that channel is marked verified.
      status          notification_id= one (full body), or to= a person's recent queue.
      claim / report  used by the notify runner (contrib/notify-runner), not by agents.

    Args:
        session_id: Your session ID
        action: send | register | humans | status | test | claim | report
        to: Recipient, "human:<name>" or "<name>"
        subject: Subject line (send)
        body: Message text you wrote (send). Plain text.
        urgency: normal | urgent (urgent ignores the person's quiet hours)
        channel: auto | gmail | slack (auto = their preferred)
        name: Person to register
        gmail / slack / email: Destinations to register for that person
        preferred: Preferred channel (register)
        quiet_hours: "HH:MM-HH:MM" local; normal messages wait until after (register)
        timezone: IANA zone for quiet hours (register)
        notification_id: For status / report
        runner_host, ok, provider_id, error, limit: runner protocol (claim/report)
    """
    err = require_session(session_id)
    if err:
        return err
    if action not in ACTIONS:
        return json.dumps({"error": f"action must be one of {list(ACTIONS)}"})
    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})
    sess = active_sessions.get(session_id) or {}
    agent, project, role = sess.get("claude_instance"), sess.get("project"), sess.get("role") or "agent"

    if action == "send":
        return json.dumps(nt.enqueue(db, to, subject, body, {"agent": agent, "project": project, "kind": "agent"},
                                     urgency=urgency, channel=channel, session_id=session_id))
    if action == "test":
        return json.dumps(nt.enqueue(
            db, to, "junto notify test",
            f"Test notification from junto, queued by {agent}@{project}. If you can read this, your notify "
            "runner and connector work; the channel is now marked verified.",
            {"agent": agent, "project": project, "kind": "test"}, urgency="urgent", channel=channel,
            session_id=session_id, test=True))
    if action == "register":
        chans = {k: v for k, v in (("gmail", gmail), ("slack", slack), ("email", email)) if v}
        return json.dumps(nt.register_human(db, name, agent, role, chans, preferred, quiet_hours, timezone),
                          default=str)
    if action == "humans":
        return json.dumps({"humans": nt.list_humans(db)}, default=str)
    if action == "status":
        return json.dumps(nt.status(db, to, notification_id, limit=limit or 20), default=str)
    if action == "claim":
        return json.dumps(nt.claim(db, name or to, runner_host or agent or "runner", limit or 5), default=str)
    # report
    if not notification_id or ok is None:
        return json.dumps({"error": "report needs notification_id and ok"})
    res = nt.report(db, notification_id, bool(ok), provider_id, error)
    if res.get("final"):
        await _tell_sender_failed(db, res)
    return json.dumps({k: v for k, v in res.items() if k not in ("sender",)}, default=str)


async def _tell_sender_failed(db, res: dict) -> None:
    """A notification that will not be retried goes back to whoever sent it."""
    sender = res.get("sender") or {}
    if not sender.get("agent") or not sender.get("project") or sender.get("kind") == "test":
        return
    from shared_memory.delegation import send_notices
    await send_notices(db, [{
        "to": [f"{sender['project']}/{sender['agent']}"], "category": "info", "kind": "notify_failed",
        "subject": f"Notification to {res['to']} FAILED: {res['subject'][:80]}",
        "body": (f"Notification {res['notification_id']} to human:{res['to']} failed after retries: "
                 f"{(res.get('error') or '')[:300]}. Their notify runner may be down or their connector "
                 "not set up. memory_notify(action='humans') shows their verified channels."),
    }])
