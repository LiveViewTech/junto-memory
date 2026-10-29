"""Pane gate of the box reporter's idle-park auto-reset (contrib/context-report)."""
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("mcp")

_PATH = Path(__file__).resolve().parents[1] / "contrib" / "context-report" / "junto-context-report.py"
_spec = importlib.util.spec_from_file_location("junto_context_report", _PATH)
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)

P = "❯"
NUDGE = "← junto-inbox: [SYSTEM NOTICE] Idle 50 min: park now (memory_get_skill('pa…"


def test_banner_after_last_prompt_ok():
    lines = [P + " do the thing", "● done", "\U0001f17f️ I AM PARKED — /clear then go", P + " "]
    assert report.analyze(lines) == (True, "ok")


def test_nudge_anchors_park_without_banner():
    lines = [P + " earlier work", "● reply", NUDGE, "  Called junto 3 times",
             "● I've parked again on the idle reminder.", "✻ Churned for 10s", P + " "]
    assert report.analyze(lines) == (True, "ok")


def test_user_prompt_after_nudge_blocks():
    lines = [NUDGE, "● parked", P + " hey, one more thing", "● sure", P + " "]
    assert report.analyze(lines) == (False, "no park banner or nudge after last prompt")


def test_no_anchor_blocks():
    lines = [P + " work", "● done", P + " "]
    assert report.analyze(lines)[0] is False


def test_busy_after_nudge_blocks():
    lines = [NUDGE, "✽ Working… (esc to interrupt)", P + " "]
    assert report.analyze(lines) == (False, "busy")


GREY = "\x1b[38;5;246m"
RESET = "\x1b[39m"


def test_grey_ghost_text_on_input_line_is_empty():
    lines = [NUDGE, "● parked", RESET + P + " " + GREY + "run the tests" + RESET]
    assert report.analyze(lines) == (True, "ok")


def test_typed_text_on_input_line_blocks():
    lines = [NUDGE, "● parked", RESET + P + " hello world"]
    assert report.analyze(lines) == (False, "input not empty")


def test_typed_text_then_ghost_completion_blocks():
    lines = [NUDGE, "● parked", RESET + P + " /cle" + GREY + "ar" + RESET]
    assert report.analyze(lines) == (False, "input not empty")


def test_right_aligned_hint_on_input_row_is_empty():
    lines = [NUDGE, "● parked", P + " " + " " * 60 + "/rc"]
    assert report.analyze(lines) == (True, "ok")


def test_grey_carried_from_previous_line():
    lines = [NUDGE, "● parked", GREY + "──────", P + " leftover hint"]
    assert report.analyze(lines) == (True, "ok")


def test_grey_submitted_prompt_still_counts_as_user_prompt():
    sub = "\x1b[38;5;239m\x1b[48;5;237m" + P + " go" + RESET
    lines = [NUDGE, "● parked", sub, "● sure", RESET + P + " "]
    assert report.analyze(lines) == (False, "no park banner or nudge after last prompt")


def test_real_idle_capture_is_empty():
    lines = ["\x1b[38;5;244m" + "─" * 20, RESET + P + " ", "\x1b[38;5;244m" + "─" * 20,
             RESET + "  " + GREY + "HA | opus | 12%" + RESET]
    assert report.analyze([NUDGE] + lines) == (True, "ok")
