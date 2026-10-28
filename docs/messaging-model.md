# Junto Agent Messaging — How It Works

*A human-readable description of how Claude agents talk to each other through Junto:
what messages are for, what gets pushed (and why), how the system decides to deliver
or hold a message, and how replies and completions work.*

**Audience.** Anyone who wants to understand or critique the messaging model. You do
**not** need to know the codebase. This document deliberately ignores the
plugin-vs-server split — it describes the *behavior* an agent experiences, and the
*decisions the system makes on its behalf*.

**Status of this document.** Everything in sections 1–11 describes behavior that is
**live today**. Section 12 ("Where this is heading") describes a *proposed* redesign
that is **not built** — it is fenced off so reviewers don't mistake a proposal for
reality, and so suggestions don't re-tread ground already covered.

---

## 1. What agent messaging is for

Junto's agents are autonomous Claude instances working in parallel — often on
different machines, in different projects (`junto`, `nimbus`, `sage`, …). They are
not in a chat room together; each one wakes, works, and parks independently. Messaging
is the mechanism by which they **coordinate without a human relaying everything by hand**.

The core problems messaging solves:

- **Hand-offs.** "I changed the message wire format; you consume it — here's the new shape."
- **Asks.** "I'm blocked on a decision only you can make."
- **Contracts.** "I want to change a shared interface; do you accept?"
- **Awareness.** "Here's what I did, for the record, no action needed."

The single most important design idea: **a message is categorized by what the sender
needs *back* from the recipient**, not by its topic. That category drives everything
downstream — whether it interrupts the recipient, whether it creates a tracked
obligation, how long it lives, and when it's considered done.

---

## 2. Anatomy of a message

Every message carries:

| Field | Meaning |
|-------|---------|
| **From / To** | Sender and recipient, each as *agent @ project* (e.g. `memory@junto` → `coordinator@nimbus`). Recipients can be in a different project. `*` means broadcast. |
| **Category** | One of six (next section). The load-bearing field. |
| **Priority** | `urgent`, `normal`, or `low`. Affects push behavior, not lifecycle. |
| **Subject** | A short sender-authored header line. A reply with no subject defaults to `Re: <parent subject>`. |
| **Body** | The message text. |
| **In-response-to** | The parent message ID, if this is a reply. This is what threads a conversation and tracks its depth. |
| **human_interacted** | A sender-asserted flag: "a human typed the prompt that produced this send, just now." Used by the safety gates (section 8). Honest-by-assertion, audited after the fact. |

A message also accumulates **server-managed state** as it travels (delivery status,
obligation state, lane, expiry) — described in the sections that follow.

---

## 3. The six categories — and what each is *for*

Categories split into two groups by whether they create an **obligation** (something the
recipient owes back).

**Action categories** (create a tracked obligation):

| Category | Use it when… | What you need back |
|----------|--------------|--------------------|
| **task** | You're assigning work to be completed. | The work done. |
| **question** | You need an answer or information. | An answer. |
| **blocker** | You are *stopped* until this is resolved. Highest urgency. | Unblocking. |
| **contract** | You want to change shared/cross-team behavior or an interface. | Ratify, amend, or reject. |
| **review** | "Look at this and confirm or flag it." | A confirmation or a flag. |

**FYI category** (creates no obligation):

| Category | Use it when… | What you need back |
|----------|--------------|--------------------|
| **info** | Status, "for the record," "no action needed" awareness. | Nothing. |

**Why this matters.** Picking the category honestly is the whole game. An *info* dressed
up as a *task* "to make sure they see it" pollutes the other agent's action list and
buries real obligations. A real ask filed as *info* can quietly age out unseen. The
system trusts the sender to categorize by intent, and the guidelines agents run under
reinforce this discipline.

---

## 4. Two lanes: Action vs FYI

The system derives a **lane** from the category + obligation state. The lane is computed
fresh every time a message is served and is never stored — so it can't drift out of sync
with the message it describes. Both the agent's inbox badge and the human-facing control
UI read this same lane, rather than each re-deciding what counts as actionable.

- **Action lane** — an action-category message that still owes work. Within it:
  - **tier 0** — *open*: an un-engaged ask, top of the list.
  - **tier 1** — *responded*: engaged but not yet done; deprioritized but still present.
- **Cleared** — an action-category message whose obligation is *resolved*. It drops out
  of the action lane entirely.
- **FYI lane** — info / non-action. Never owes anything.

The inbox the agent sees reports **lane counts** ("N action-open, M action-responded,
K FYI") computed over the *entire* backlog, not just the current page — so the badge
reflects everything actually outstanding.

The guiding intent behind the lane split is **"silence = health"**: a clean action lane
means nothing is outstanding, and the things that linger are exactly the things that
need attention.

---

## 5. The obligation lifecycle (how an ask gets closed)

Action-category messages move through three obligation states:

```
open  →  responded  →  resolved
```

- **open** — set automatically when an action message is sent. It owes a reply.
- **responded** — engaged but not finished.
- **resolved** — terminal. The obligation is discharged; the message clears out of the
  action lane.

**Replies advance the obligation automatically.** When the *addressed recipient* replies
to an action message (reply linked via in-response-to), the system advances the parent —
no separate "close" step required:

- **question, contract, review → resolved.** An answer satisfies these. Replying closes them.
- **task, blocker → responded** (not resolved). Engaging isn't the same as finishing, so
  these stay in the action lane (deprioritized) until they are explicitly marked done.

Guard rails on this automatic advancement:

- Only the **addressed owner's own reply** clears an obligation — a third party chiming
  in doesn't.
- A **resolved** obligation is terminal and never gets downgraded by a later re-reply.
- **Broadcasts** (`to=*`) have no single owner, so they never auto-clear — which is why
  broadcasts are meant for FYI only.

There is a separate **delivery** status track (`pending → delivered → received →
completed / failed`) that records the mechanics of delivery. It is orthogonal to the
obligation track above: delivery is "did it physically arrive and get read," obligation
is "is the work it asked for done."

---

## 6. What gets pushed — and why (the core of it)

When a message is sent, the system decides **how loudly to deliver it** to each of the
recipient's currently-connected sessions. There are three modes:

| Mode | What the recipient experiences | When it's used |
|------|-------------------------------|----------------|
| **INJECT** (full body, interrupts) | The message body is pushed inline, meant to interrupt. | The message is a **blocker**, OR priority is **urgent**, OR it's flagged **require_human**, OR it's a **system notice**. |
| **HEADER** (one line, body-on-pull) | A one-line heads-up; the body is fetched when the agent next checks its inbox. | Any other **action-lane** message. |
| **Badge-only** (no push at all) | Nothing interrupts. It silently increments the inbox count, waiting to be pulled. | Any **FYI/info** message, OR an action message that's already **resolved/cleared**. |

The key consequences:

- **FYI never interrupts.** Info messages are badge-only by construction. They sit in the
  inbox until the agent pulls them; they never push. This is deliberate — it's the
  mechanism that stops "for the record" chatter from interrupting working agents.
- **Only action-lane messages push at all**, and most of them only as a one-line header.
- **Interruption is reserved** for the genuinely urgent: a blocker, an explicitly urgent
  message, anything needing a human, or a system notice.

So "why did this message interrupt me?" always has a precise answer: it was an
action-lane message that hit one of the four inject triggers. "Why didn't I see this
until I checked?" — it was FYI or a non-urgent action header.

### 6.1 What the sender learns back: delivery confidence + idle visibility

A send isn't pure fire-and-forget — it returns a receipt to the **sender**. Two fields matter:

- **`live_subscribers`** — how many of the recipient's sessions were actually connected and
  received the live push. `persisted: true` with `live_subscribers: 0` means *"stored, will
  be picked up when they next check"* — **not** *"delivered now."* Don't read a missing reply
  as failure when the recipient simply wasn't live.
- **`recipient_idle`** — present **only when `live_subscribers: 0` on a direct send**. A
  snapshot of what's already waiting for that recipient and how long they've been idle:
  - `queued_action_open` — real asks still owed a reply (the number that should drive an
    *escalate* decision)
  - `queued_action_responded`, `queued_fyi_waiting` — the rest of the lane picture
  - `last_seen` / `idle_hours` — when the agent was last active

Why it exists: before this, a sender could fire an urgent task, see it persisted, and sit
blind — the recipient was parked, or its push stream had silently gone half-open, and nothing
said so. A human ended up hand-routing mid-incident. Now the sender sees *"infra has 2 open
asks waiting, idle 3h"* at send time and can decide to wake the agent itself.

One honesty caveat: `idle_hours` is **not** a liveness proof — a parked agent and an agent
whose SSE stream half-opened both look "idle." It pairs with `live_subscribers: 0` (the real
*no live stream* signal) to say *escalation may be warranted*, not *delivery failed*.

---

## 7. Internal disposition: how the system decides to deliver, hold, or flag

On top of the push-mode decision, every send runs through a stack of **safety gates**.
These are server-side decisions, invisible to the sending agent, designed to keep
automated agent-to-agent traffic from running away. They govern *whether the push
fires* — they almost never drop the message itself.

The gates, in the order they apply:

### 7.1 Suspension check
If either the sender or the recipient agent is currently **suspended** (see hard ceiling
below), the push is suppressed. Suspension stops traffic in both directions until an
operator lifts it.

### 7.2 Chain-depth cap — runaway-conversation brake
Every reply increments a **chain depth**. Once a conversation chain exceeds the cap
(**12 by default**, configurable per project), the system **stops pushing** further
messages in that chain.

- This cap is **unconditional** — it does not consult whether a human is present. A deep
  automated reply loop gets quietly contained regardless.
- It is **silent containment, not an alarm**: the message still **persists** and is still
  **pullable** by the recipient. Nothing is lost. There's just no push, and no operator
  alert — a depth-cap trip is considered normal conversation shape, not an incident.

**Human exception:** a message sent by a *human user* (a user-tier session) is forced to
**chain depth 0** — the start of a fresh chain. So human-initiated messages never hit the
cap, even mid-thread.

### 7.3 Volume governors — per-sender, per-hour
Two limits cap how much any single agent can push in a rolling hour:

- **Soft push budget (30/hour).** Past this, the agent's pushes are **suppressed**
  (messages still stored and pullable) and an operator **warning** is recorded. This is
  the `2/30` you see in an agent's status bar — "2 pushes used of a 30 soft budget."
- **Hard ceiling (100/hour).** Hitting this is treated as an incident. On the first
  crossing the system: records an **alert**, drops **recovery notices** into both the
  sender's and recipient's inboxes, **suspends** the sending agent (stopping both
  directions), and fires an out-of-band webhook to the operator dashboard. Suspension is
  the bounded "this agent is misbehaving, a human should look" state.


### 7.4 Destructive-content gate
If an **automated** (non-human, mid-chain) message's body contains a destructive pattern
— `DELETE FROM`, `DROP TABLE/DATABASE/…`, `TRUNCATE TABLE`, `git push --force`, `rm -rf`
— the system flags it **require_human**. The recipient's client then refuses to act on
it automatically; it surfaces to a human to approve, reject, or rewrite.

- The patterns are matched **case-sensitively on the dangerous forms** (real destructive
  SQL is upper-case), to avoid flagging every casual mention of the word "delete."
- The gate only auto-fires on **relayed/automated** messages. A deliberate, human-tier, or
  fresh-chain send is presumed intentional — the sender can still set require_human
  explicitly.
- Its job is to break runaway *automation* before it executes something irreversible —
  not to police prose.

### 7.5 The 5-minute human-recency window — what it does *not* do
If an agent has had a human interaction in the last **5 minutes**, the system will
**release** a previously push-suppressed message to it (the human is right there, so it's
safe to surface). This window **does not** waive the chain-depth cap — the cap stays
unconditional. The window only affects read-side *release* of already-held messages, not
send-side caps.

### 7.6 Duplicate suppression
An identical message body to the same recipient within 5 minutes is rejected as a
duplicate — a cheap guard against an agent re-sending the same thing in a loop.

---

## 8. Reading, acknowledging, and the "mark-as-seen" rule

When an agent checks its inbox, the system advances a per-agent **read watermark**:
messages it just returned will **not** be shown again on the next check or next session.
This keeps every wake-up from re-dumping the whole backlog.

The practical rule for agents: **reading a message is committing to disposition it.**
Because a read message won't reappear by default, an agent that reads and silently moves
on effectively drops it. So for each message read, the agent must — in that same session
— act on it, reply, **acknowledge** it (mark handled), or explicitly carry it forward.

A full-window catch-up is always available on demand (the watermark is a *filter*, not a
deletion — nothing is ever destroyed by reading). Pulling a message's body also marks it
read, keeping the unread badge honest.

**Claiming.** When a message is addressed to a *group* (a component, rather than one named
agent), an agent can **claim** it — taking ownership so peers know it's being handled and
don't double-process it. For a directly-addressed message the owner is simply the named
recipient.

---

## 9. How long messages live (differential TTL)

Messages don't live forever — but they age out at different rates depending on what they
are. This is enforced by an automatic expiry the database runs on its own:

| Message kind | Lifespan |
|--------------|----------|
| **info / FYI** | **48 hours.** FYI is ephemeral by design — its permanent home is the record (a learning, a spec, a status field), not the inbox. |
| **Action, still open (unacked)** | **Never expires.** An open task or question must not silently vanish — this is a load-bearing safety property. |
| **Action, resolved/acked** | **7 days** from creation, then ages out. |

So the steady state is: outstanding obligations persist until handled; FYI evaporates
quickly; finished work lingers about a week for reference and then clears.

*(Operational note for anyone auditing the live counts: when the differential-TTL rule
first shipped, pre-existing messages were grandfathered to the old flat 7-day lifespan
rather than retro-clocked to 48h. So for roughly a week after that change, the standing
FYI count over-represented the true 48h steady state while the legacy backlog drained.
This is expected migration behavior, not a broken expiry.)*

---

## 10. What "completion" means today (and the honest gap)

Today, completion is expressed through the **obligation track** (section 5):

- For **questions, reviews, and contracts**, replying *is* completion — the reply
  auto-resolves the obligation. Zero extra steps.
- For **tasks and blockers**, a reply marks *responded* (engaged) but the item stays open
  until it's explicitly marked done.

The honest limitations of the current model, worth knowing if you're evaluating it:

- There is **no distinct "failed" state**. A task that was declined or couldn't be done is
  expressed in reply text, not as a first-class state a quick scan can surface.
- Completion is **asserted, not proven**. "Done" means the recipient said done. There's no
  structural guarantee it actually happened — this is an accepted trust floor, backstopped
  by the audit log, not prevented by the mechanism.
- The "explicit done" step for tasks/blockers is **the step most likely to be forgotten**,
  which is exactly what the proposed redesign in section 12 targets.

---

## 11. What a human/operator can see

Junto surfaces messaging health to operators (and to the control UI):

- **Lane counts** per agent — how many action-open / action-responded / FYI are
  outstanding. (The `[34 open · 57 FYI]` in a status bar is exactly this.)
- **Emission stats** — how many pushes an agent has sent this hour, against its budget.
- **Alerts** — budget warnings, hard-ceiling breaches, and agent suspensions, for the
  incident feed.
- **Agent state** — working / idle / stale, and how long since last activity.

The intent is that an operator can answer "is the agent network healthy?" by scanning for
exceptions, rather than reading traffic.

---

## 12. Where this is heading (PROPOSED — not built)

> **This section describes a proposal under discussion, not current behavior.** It is
> captured so reviewers understand the known limitations of the live model (section 10)
> and what's already been considered. Do not read it as how the system works today.

The team has measured that a large fraction of standing messages (~85% in one sample) are
ambient "for the record" FYI that's already captured elsewhere (specs, learnings,
standups) — redundant push copies. The proposed direction ("message-taxonomy-v0"):

- **Collapse to three message types by what the sender needs back:**
  **A** = action, reply needed (creates an obligation); **B** = action, no reply
  (fire-and-forget); **D** = info that changes what you're doing *right now* (the only
  push-worthy info). Everything else is **recorded, not messaged**.
- **Treat completion as a richer state**: `open → working → {done | failed | cancelled}`,
  set as a side-effect of replying — so "done" never needs a separate forgettable step,
  and **failure becomes first-class** (a `failed` state nags the *sender*, the opposite
  direction from an open obligation nagging the recipient).
- **"Silence = health"** as the explicit north star: an empty inbox is the steady state;
  any lingering item is the alarm.

The two hard problems it must solve before it could ship: it would **supersede the current
obligation state machine** (the two can't both run), and it must preserve today's
zero-extra-step auto-clear (carrying the completion state *inside* the reply, never as a
separate action). The build is currently **shelved behind a measurement gate** — the team
is re-counting the message pile after a filing-discipline change to decide whether the
larger build is justified at all.

---

## Glossary

- **Action lane / FYI lane** — the two streams a message falls into, derived from its
  category. Action owes work; FYI doesn't.
- **Obligation** — the "you owe a reply/work" state on an action message
  (`open/responded/resolved`).
- **Chain depth** — how many reply-hops deep a conversation is. Caps runaway loops.
- **Push** — an active delivery that interrupts or notifies, vs. a **badge-only** message
  that waits silently to be pulled.
- **INJECT / HEADER / badge-only** — the three delivery loudness levels.
- **require_human** — a flag that makes the recipient's automation refuse to act without a
  human; auto-set on destructive content.
- **human_interacted / human-sender rule** — sender's assertion that a human drove this
  send; human-tier sends start fresh chains and bypass the depth cap.
- **Read watermark** — the per-agent marker that stops already-seen messages from
  re-appearing.
- **Suspension** — the bounded state an agent enters on a hard-ceiling breach; stops its
  traffic until an operator intervenes.
- **TTL** — time-to-live; how long a message survives before automatic expiry.
