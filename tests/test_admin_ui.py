"""Junto Admin web UI back end (admin_ui.py)."""

import shared_memory.admin_ui as ui


class _Col:
    def __init__(self):
        self.docs = []

    def find_one(self, q, proj=None):
        return next((d for d in self.docs if all(d.get(k) == v for k, v in q.items())), None)

    def update_one(self, q, change, upsert=False):
        d = self.find_one(q)
        if d is None:
            d = dict(q)
            self.docs.append(d)
        d.update(change.get("$set", {}))
        for k in change.get("$unset", {}):
            d.pop(k, None)

    def insert_one(self, doc):
        self.docs.append(doc)


class _DB:
    def __init__(self):
        self.push_control_config = _Col()
        self.query_config = _Col()
        self.config_changes = _Col()


def _patch_audit(monkeypatch):
    import shared_memory.push_control as pc
    import shared_memory.query_config as qc
    for mod in (pc, qc):
        if hasattr(mod, "log_audit"):
            monkeypatch.setattr(mod, "log_audit", lambda *a, **k: None)


def test_settings_payload_lists_every_key_with_layers():
    db = _DB()
    db.push_control_config.docs.append({"scope": "nimbus", "push_budget": 45})
    out = ui.settings_payload(db, "nimbus")
    pc = next(s for s in out["sections"] if s["id"] == "push_control")
    row = next(r for r in pc["settings"] if r["key"] == "push_budget")
    assert row["code_default"] == 30 and row["project_value"] == 45 and row["effective"] == 45
    lapse = next(r for r in pc["settings"] if r["key"] == "action_lapse_days")
    assert lapse["description"] and lapse["project_value"] is None
    qc = next(s for s in out["sections"] if s["id"] == "query_config")
    assert any(r["key"] == "recall_floor" for r in qc["settings"])


def test_every_setting_has_a_description():
    for sec in ui._sections().values():
        for key in sec["keys"]:
            assert ui.DESCRIPTIONS.get(key), f"missing DESCRIPTIONS[{key!r}]"


def test_apply_setting_writes_and_logs_change(monkeypatch):
    _patch_audit(monkeypatch)
    db = _DB()
    r = ui.apply_setting(db, "admin-ui:tom", {"section": "query_config", "key": "recall_floor",
                                              "value": 0.65, "project": "nimbus"})
    assert r["ok"] and r["effective_before"] == 0.6 and r["effective_after"] == 0.65
    ch = db.config_changes.docs[0]
    assert ch["actor"] == "admin-ui:tom" and ch["scope"] == "nimbus" and ch["effective_after"] == 0.65


def test_apply_setting_rejects_unknown_and_validates(monkeypatch):
    _patch_audit(monkeypatch)
    db = _DB()
    assert "error" in ui.apply_setting(db, "x", {"section": "nope", "key": "a", "value": 1})
    assert "error" in ui.apply_setting(db, "x", {"section": "push_control", "key": "bogus", "value": 1})
    bad = ui.apply_setting(db, "x", {"section": "query_config", "key": "recall_floor", "value": 3.0})
    assert "error" in bad and not db.config_changes.docs
    assert "error" in ui.apply_setting(db, "x", {"section": "push_control", "key": "push_budget", "reset": True})


def test_secret_values_are_masked():
    assert ui._mask("webhook_token", "abcdef123456") == "••••3456"
    assert ui._mask("push_budget", 30) == 30


class _Req:
    def __init__(self, headers):
        self.headers = type("H", (), {"raw": [(k.encode(), v.encode()) for k, v in headers.items()]})()


def test_auth_requires_owner_and_refuses_tunnel(monkeypatch):
    import shared_memory.auth as auth
    keys = {"own": {"name": "tom", "role": "owner"}, "agt": {"name": "bot", "role": "agent"}}
    monkeypatch.setattr(auth, "validate_api_key", lambda k: keys.get(k))
    assert ui._auth(_Req({"Authorization": "Bearer own"}))["ok"]
    a = ui._auth(_Req({"Authorization": "Bearer agt"}))
    assert not a["ok"] and a["status"] == 403 and a["name"] == "bot"
    assert ui._auth(_Req({}))["status"] == 401
    t = ui._auth(_Req({"Authorization": "Bearer own", "CF-Connecting-IP": "1.2.3.4"}))
    assert not t["ok"] and t["error"] == "admin is LAN-only"


def test_reads_need_no_key_by_default(monkeypatch):
    import shared_memory.auth as auth
    monkeypatch.setattr(auth, "REQUIRE_KEY", False)
    assert ui._lan_only(_Req({})) is None
    assert ui._lan_only(_Req({"CF-Connecting-IP": "1.2.3.4"})).status_code == 403


def test_reads_need_a_valid_key_when_require_key(monkeypatch):
    # Tailscale-only work box: no tunnel header, so origin can't gate reads.
    import shared_memory.auth as auth
    monkeypatch.setattr(auth, "REQUIRE_KEY", True)
    monkeypatch.setattr(auth, "validate_api_key", lambda k: {"name": "eric", "role": "agent"} if k == "good" else None)
    assert ui._lan_only(_Req({})).status_code == 401
    assert ui._lan_only(_Req({"Authorization": "Bearer bad"})).status_code == 401
    assert ui._lan_only(_Req({"Authorization": "Bearer good"})) is None
