"""State-spec write guard (interface:state-spec-write-guard-v0, backlog_889bbe240666).

define_spec replaces content, so a rule an agent omits at park is deleted
silently. The guard reports rule-shaped standing fragments that vanished —
advisory, never blocking. Real case: state:server-team 1.1.228 → 1.1.229
lost "NEVER `-Recurse` D:\\Media".
"""

import json

from shared_memory import state_guard
from shared_memory.tools import specs
from test_get_spec_resolution import _FakeCol, _install, _patch_chroma, _spec_meta

OLD = """## Current Task
Shipping NEVER-related fix (Status churns; never diffed).

## Key Context — POINTERS
- ⚠️ Ops: stargate `sudo mysql`=SELECT-ONLY · build Release · NEVER `-Recurse` D:\\Media · ssh key C:\\keys\\id
- Plain fact about the web tier that changes often.
### Don't repeat
- Do not edit the cache; edit the DEV repo.

## Next Steps
1. ALWAYS check this one (Next Steps are not standing).
"""


def test_reports_dropped_rule_fragment_only():
    new = OLD.replace(" · NEVER `-Recurse` D:\\Media", "").replace(
        "- Plain fact about the web tier that changes often.\n", "")
    assert state_guard.dropped_standing(OLD, new) == ["NEVER `-Recurse` D:\\Media"]


def test_moved_or_reformatted_fragment_is_not_dropped():
    new = OLD.replace(" · NEVER `-Recurse` D:\\Media", "") + "\n* never   `-recurse` d:\\media\n"
    assert state_guard.dropped_standing(OLD, new) == []


def test_nested_dont_repeat_section_and_next_steps_ignored():
    new = OLD.replace("- Do not edit the cache; edit the DEV repo.\n", "").replace(
        "1. ALWAYS check this one (Next Steps are not standing).\n", "")
    assert state_guard.dropped_standing(OLD, new) == ["Do not edit the cache"]


def test_unchanged_spec_reports_nothing():
    assert state_guard.dropped_standing(OLD, OLD) == []


async def _seed_state(monkeypatch, content):
    cols = {}
    _patch_chroma(monkeypatch, cols)
    proj = _FakeCol("proj_nimbus")
    await proj.upsert(["spec_state_frames-team"], [content],
                      [_spec_meta("1.0.4", "state", "active", "nimbus")])
    cols["proj_nimbus"] = proj
    cols["shared_patterns"] = _FakeCol("shared_patterns")
    _install()
    return proj


async def test_define_spec_returns_dropped_standing_and_saves(monkeypatch):
    proj = await _seed_state(monkeypatch, OLD)
    new = OLD.replace(" · NEVER `-Recurse` D:\\Media", "")
    res = json.loads(await specs.memory_define_spec(
        session_id="s1", name="state:frames-team", content=new,
        spec_type="state", project="nimbus"))
    assert res["status"] == "updated", res
    assert res["dropped_standing"] == ["NEVER `-Recurse` D:\\Media"]
    assert res["dropped_standing_count"] == 1
    assert "Advisory" in res["dropped_standing_note"]
    assert proj.docs["spec_state_frames-team"][0] == new


async def test_define_spec_clean_update_has_no_advisory(monkeypatch):
    await _seed_state(monkeypatch, OLD)
    res = json.loads(await specs.memory_define_spec(
        session_id="s1", name="state:frames-team", content=OLD + "\nmore\n",
        spec_type="state", project="nimbus"))
    assert res["status"] == "updated", res
    assert "dropped_standing" not in res


async def test_non_state_spec_not_guarded(monkeypatch):
    cols = {}
    _patch_chroma(monkeypatch, cols)
    proj = _FakeCol("proj_nimbus")
    await proj.upsert(["spec_design_x"], [OLD],
                      [_spec_meta("1.0.0", "design", "active", "nimbus", name="design:x")])
    cols["proj_nimbus"] = proj
    cols["shared_patterns"] = _FakeCol("shared_patterns")
    _install()
    res = json.loads(await specs.memory_define_spec(
        session_id="s1", name="design:x", content="short", spec_type="design",
        project="nimbus"))
    assert "dropped_standing" not in res


def test_edit_to_fact_sentence_keeps_rule_on_same_line():
    from shared_memory.state_guard import dropped_standing
    old = "## Key Context\n- What's live: run `live.sh`. Never write a SHA here.\n"
    new = "## Key Context\n- What's live: run `bash live.sh --prod`. Never write a SHA here.\n"
    assert dropped_standing(old, new) == []


def test_reworded_rule_is_kept_but_removed_rule_is_reported():
    from shared_memory.state_guard import dropped_standing
    old = "## Key Context\n- NEVER run the mid-period sweep manually on prod.\n- Never put the customer email on the ship-to.\n"
    new = "## Key Context\n- Never run the mid-period sweep manually on prod (Tom only).\n"
    assert dropped_standing(old, new) == ["Never put the customer email on the ship-to."]
