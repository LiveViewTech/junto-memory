"""Case-variant spec forks (backlog_d3fb15db8acd, nimbus coordinator 2026-08-02).

Spec doc ids derive from the name verbatim, so `db:Accounting.sales` beside an
existing `db:Accounting.Sales` silently created a second spec. Ten nimbus
schema specs forked this way in one session. define_spec now refuses a
case-only collision unless force=True; get_spec misses suggest the twin.
"""

import json

from shared_memory.tools import specs
from test_get_spec_resolution import _FakeCol, _install, _patch_chroma, _spec_meta


async def _seed(monkeypatch, name="db:Accounting.Sales", status="active"):
    cols = {}
    _patch_chroma(monkeypatch, cols)
    proj = _FakeCol("proj_nimbus")
    doc_id = "spec_" + name.replace(":", "_").replace("/", "_")
    await proj.upsert([doc_id], ["canonical schema"],
                      [_spec_meta("1.0.4", "schema", status, "nimbus", name=name)])
    cols["proj_nimbus"] = proj
    cols["shared_patterns"] = _FakeCol("shared_patterns")
    _install()
    return proj


async def test_define_spec_refuses_case_variant(monkeypatch):
    proj = await _seed(monkeypatch)
    res = json.loads(await specs.memory_define_spec(
        session_id="s1", name="db:accounting.sales", content="rebuilt",
        spec_type="schema", project="nimbus"))
    assert res.get("error") == "Case-variant spec name collision", res
    assert res["existing"] == ["db:Accounting.Sales"]
    assert "spec_db_accounting.sales" not in proj.docs


async def test_define_spec_force_creates_case_variant(monkeypatch):
    proj = await _seed(monkeypatch)
    res = json.loads(await specs.memory_define_spec(
        session_id="s1", name="db:accounting.sales", content="separate on purpose",
        spec_type="schema", project="nimbus", force=True))
    assert "error" not in res, res
    assert "spec_db_accounting.sales" in proj.docs


async def test_define_spec_exact_name_still_updates(monkeypatch):
    await _seed(monkeypatch)
    res = json.loads(await specs.memory_define_spec(
        session_id="s1", name="db:Accounting.Sales", content="v5",
        spec_type="schema", project="nimbus", owner="frames-team"))
    assert "error" not in res, res


async def test_archived_case_variant_does_not_block(monkeypatch):
    await _seed(monkeypatch, status="archived")
    res = json.loads(await specs.memory_define_spec(
        session_id="s1", name="db:accounting.sales", content="new",
        spec_type="schema", project="nimbus"))
    assert "error" not in res, res


async def test_get_spec_miss_suggests_case_variant(monkeypatch):
    await _seed(monkeypatch)
    res = json.loads(await specs.memory_get_spec(
        session_id="s1", name="db:accounting.sales", project="nimbus"))
    assert "not found" in res["error"]
    assert res["did_you_mean"] == ["db:Accounting.Sales"]


async def test_get_spec_plain_miss_unchanged(monkeypatch):
    await _seed(monkeypatch)
    res = json.loads(await specs.memory_get_spec(
        session_id="s1", name="db:Nope", project="nimbus"))
    assert "did_you_mean" not in res
    assert res["suggestion"] == "Use memory_list_specs to see available specs"
