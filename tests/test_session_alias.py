"""junto_session → session_id alias (msg_50ff99f0ead1): Claude desktop strips
any tool argument named exactly session_id."""

from types import SimpleNamespace

import pytest
from mcp import types

from shared_memory.intent import build_call_tool_handler_with_session_alias
from shared_memory.tool_profiles import advertise_session_alias


def _req(args):
    return SimpleNamespace(params=SimpleNamespace(name="memory_query", arguments=args))


async def _seen(args):
    out = {}

    async def inner(req):
        out.update(req.params.arguments)
        return "ok"
    await build_call_tool_handler_with_session_alias(inner)(_req(args))
    return out


@pytest.mark.asyncio
async def test_alias_becomes_session_id():
    assert await _seen({"junto_session": "s1", "query": "q"}) == {"session_id": "s1", "query": "q"}


@pytest.mark.asyncio
async def test_explicit_session_id_wins_and_alias_is_dropped():
    assert await _seen({"session_id": "real", "junto_session": "other"}) == {"session_id": "real"}


@pytest.mark.asyncio
async def test_plain_session_id_untouched():
    assert await _seen({"session_id": "s"}) == {"session_id": "s"}


@pytest.mark.asyncio
async def test_empty_alias_is_ignored():
    assert await _seen({"junto_session": ""}) == {}


def _tool(props, required):
    return types.Tool(name="memory_query", description="Args:\n    session_id: Your session ID",
                      inputSchema={"type": "object", "properties": props, "required": required})


def test_discussion_schema_renames_session_id_in_place():
    t = _tool({"session_id": {"type": "string"}, "query": {"type": "string"}}, ["session_id", "query"])
    out = advertise_session_alias(t)
    assert list(out.inputSchema["properties"]) == ["junto_session", "query"]
    assert out.inputSchema["required"] == ["junto_session", "query"]
    assert "junto_session:" in out.description
    assert "session_id" in t.inputSchema["properties"]          # original not mutated


def test_tools_without_session_id_pass_through():
    t = _tool({"project": {"type": "string"}}, ["project"])
    assert advertise_session_alias(t) is t
