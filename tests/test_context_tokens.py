"""Tests for the MCP-layer __context_tokens extraction (shared_memory.intent).

Same chokepoint as __intent_id; see test_intent_id.py for the base wrapper
behavior. These cover the second sideband kwarg and its coercion rules.
"""

from mcp import types as _mcp_types

from shared_memory.intent import (
    CONTEXT_TOKENS_KWARG,
    INTENT_ID_KWARG,
    _coerce_context_tokens,
    build_call_tool_handler_with_intent,
    get_current_context_tokens,
    get_current_intent_id,
)


def _make_request(arguments):
    return _mcp_types.CallToolRequest(
        method="tools/call",
        params=_mcp_types.CallToolRequestParams(name="t", arguments=arguments),
    )


async def _capture(observed):
    """Return a fake inner handler that records what it saw."""

    async def inner(req):
        observed["context_tokens_in_call"] = get_current_context_tokens()
        observed["intent_id_in_call"] = get_current_intent_id()
        observed["args_in_call"] = dict(req.params.arguments or {})
        return "ok"

    return inner


async def test_context_tokens_absent_is_none():
    observed: dict = {}
    handler = build_call_tool_handler_with_intent(await _capture(observed))
    await handler(_make_request({"foo": "bar"}))
    assert observed["context_tokens_in_call"] is None
    assert observed["args_in_call"] == {"foo": "bar"}


async def test_context_tokens_visible_and_stripped():
    observed: dict = {}
    handler = build_call_tool_handler_with_intent(await _capture(observed))
    await handler(_make_request({"foo": "bar", CONTEXT_TOKENS_KWARG: 123456}))
    assert observed["context_tokens_in_call"] == 123456
    assert CONTEXT_TOKENS_KWARG not in observed["args_in_call"]
    assert observed["args_in_call"] == {"foo": "bar"}


async def test_context_tokens_digit_string_coerced():
    observed: dict = {}
    handler = build_call_tool_handler_with_intent(await _capture(observed))
    await handler(_make_request({CONTEXT_TOKENS_KWARG: "98765"}))
    assert observed["context_tokens_in_call"] == 98765


async def test_context_tokens_reset_after_call():
    observed: dict = {}
    handler = build_call_tool_handler_with_intent(await _capture(observed))
    await handler(_make_request({CONTEXT_TOKENS_KWARG: 42}))
    # Leaking the contextvar would contaminate later calls — must be reset.
    assert get_current_context_tokens() is None


async def test_context_tokens_reset_after_inner_raises():
    async def boom(_req):
        raise RuntimeError("inner failure")

    handler = build_call_tool_handler_with_intent(boom)
    raised = False
    try:
        await handler(_make_request({CONTEXT_TOKENS_KWARG: 42}))
    except RuntimeError:
        raised = True
    assert raised
    assert get_current_context_tokens() is None


async def test_both_sideband_kwargs_extracted_together():
    observed: dict = {}
    handler = build_call_tool_handler_with_intent(await _capture(observed))
    await handler(_make_request({
        "foo": "bar",
        INTENT_ID_KWARG: "uuid-xyz",
        CONTEXT_TOKENS_KWARG: 777,
    }))
    assert observed["intent_id_in_call"] == "uuid-xyz"
    assert observed["context_tokens_in_call"] == 777
    assert observed["args_in_call"] == {"foo": "bar"}


def test_coerce_rejects_malformed():
    # Instrumenting a write must never block it — malformed drops to None.
    assert _coerce_context_tokens(True) is None
    assert _coerce_context_tokens(False) is None
    assert _coerce_context_tokens(0) is None
    assert _coerce_context_tokens(-5) is None
    assert _coerce_context_tokens("12.5") is None
    assert _coerce_context_tokens("-3") is None
    assert _coerce_context_tokens("") is None
    assert _coerce_context_tokens(None) is None
    assert _coerce_context_tokens([123]) is None
    assert _coerce_context_tokens(3.7) is None


def test_coerce_accepts_positive():
    assert _coerce_context_tokens(1) == 1
    assert _coerce_context_tokens(850_000) == 850_000
    assert _coerce_context_tokens("850000") == 850_000
