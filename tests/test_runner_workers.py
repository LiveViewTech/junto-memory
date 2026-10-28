"""Runner workers (contrib/runner): parallel claim loops + the exclusive-model gate."""
import importlib.util
import threading
import time
import urllib.error
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "contrib" / "runner" / "junto-runner.py"
_spec = importlib.util.spec_from_file_location("junto_runner", _PATH)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

GB = 1_000_000_000


def _cfg(sizes):
    return {"ollama": "http://127.0.0.1:9", "exclusive_model_gb": 24, "_model_sizes": dict(sizes),
            "poll_seconds": 0}


def test_exclusive_model_by_size():
    cfg = _cfg({"qwen2.5:72b": 47 * GB, "gpt-oss:20b": 13 * GB})
    assert runner.exclusive_model(cfg, "qwen2.5:72b") is True
    assert runner.exclusive_model(cfg, "gpt-oss:20b") is False


def test_unknown_model_shares(monkeypatch):
    monkeypatch.setattr(runner, "ollama_models", lambda cfg: {})
    assert runner.exclusive_model(_cfg({}), "new:model") is False


def test_unknown_model_refreshes_sizes(monkeypatch):
    monkeypatch.setattr(runner, "ollama_models", lambda cfg: {"qwen2.5:72b": 47 * GB})
    assert runner.exclusive_model(_cfg({}), "qwen2.5:72b") is True


def _track(gate, exclusive, log, hold=0.05):
    with gate.hold(exclusive):
        log.append(("in", exclusive))
        time.sleep(hold)
        log.append(("out", exclusive))


def test_shared_runs_overlap():
    gate, log = runner.RunGate(), []
    ts = [threading.Thread(target=_track, args=(gate, False, log)) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert [e for e, _ in log[:3]] == ["in", "in", "in"]


def test_exclusive_runs_alone_and_blocks_new_shared():
    gate, log = runner.RunGate(), []
    first = threading.Thread(target=_track, args=(gate, False, log, 0.2))
    first.start()
    time.sleep(0.05)
    excl = threading.Thread(target=_track, args=(gate, True, log, 0.1))
    excl.start()
    time.sleep(0.05)
    late = threading.Thread(target=_track, args=(gate, False, log, 0.05))
    late.start()
    for t in (first, excl, late):
        t.join()
    # The exclusive run waits for the running shared one, and the late shared run waits
    # behind the exclusive one instead of starving it.
    assert log == [("in", False), ("out", False), ("in", True), ("out", True), ("in", False), ("out", False)]


def test_gate_released_on_error():
    gate = runner.RunGate()
    try:
        with gate.hold(True):
            raise ValueError("boom")
    except ValueError:
        pass
    with gate.hold(False):
        assert gate.shared == 1


class _DownServer:
    def call(self, op, body=None, timeout=60):
        raise urllib.error.URLError("connection refused")


def test_work_survives_unreachable_server():
    # once=True returns after one poll; before workers, this exception killed the process.
    runner.work(_cfg({}), _DownServer(), runner.RunGate(), once=True)


class _OneRunServer:
    def __init__(self):
        self.calls = []

    def call(self, op, body=None, timeout=60):
        self.calls.append(op)
        if op == "claim":
            return {"run": {"run_id": "r1"}, "task": {"name": "t"}}
        return {}


def test_work_reports_failed_run(monkeypatch):
    def boom(cfg, server, claim, gate):
        raise RuntimeError("model down")

    monkeypatch.setattr(runner, "process", boom)
    server = _OneRunServer()
    runner.work(_cfg({}), server, runner.RunGate(), once=True)
    assert server.calls == ["claim", "fail"]
