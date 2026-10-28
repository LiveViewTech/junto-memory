"""MCP-layer sideband-kwarg extraction (__intent_id, __context_tokens).

Clients (notably the junto-inbox channel plugin) may pass sideband keyword
arguments on any tool call. The MCP entry-point wrapper in app.py pops them
from incoming arguments and stashes them on contextvars for the duration of
the call, so they never appear in per-tool argument schemas.

- `__intent_id` (str): local-journal reconciliation UUID. Phase 1 op-log
  writers read it via get_current_intent_id() and record it on the op-log
  payload (intent_id = UUID match against the client journal).
  See design:local-first-junto-v0-mvp §4 (op-log payload) and §8 (journal).
- `__context_tokens` (int): the caller session's context depth in tokens at
  call time, injected client-side by the inbox plugin's hook (the server
  cannot read a box's statusline payload). memory_record_learning stamps it
  into learning metadata for the correction-rate study's session-age axis
  (server-team thread 2026-07-30; session_id stamping is the coarse
  fallback). Absent or malformed values are silently dropped — instrumenting
  a write must never block it.
"""

import contextvars

INTENT_ID_KWARG = "__intent_id"
CONTEXT_TOKENS_KWARG = "__context_tokens"

_intent_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "intent_id", default=None
)
_context_tokens_ctx: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "context_tokens", default=None
)


def get_current_intent_id() -> str | None:
    """Return the __intent_id passed by the current MCP tool caller, if any."""
    return _intent_id_ctx.get()


def get_current_context_tokens() -> int | None:
    """Return the __context_tokens passed by the current MCP tool caller, if any."""
    return _context_tokens_ctx.get()


def _coerce_context_tokens(candidate) -> int | None:
    """Positive int (or digit-string, since hook JSON may stringify) or None.

    bool is an int subclass — reject it explicitly.
    """
    if isinstance(candidate, bool):
        return None
    if isinstance(candidate, int):
        return candidate if candidate > 0 else None
    if isinstance(candidate, str) and candidate.isdigit():
        value = int(candidate)
        return value if value > 0 else None
    return None


def _set_intent_id(value: str | None) -> contextvars.Token:
    return _intent_id_ctx.set(value)


def _reset_intent_id(token: contextvars.Token) -> None:
    _intent_id_ctx.reset(token)


def build_call_tool_handler_with_intent(orig_handler):
    """Wrap an MCP CallToolRequest handler so it extracts __intent_id from
    incoming arguments into the intent_id contextvar before delegating.

    The wrapper mutates req.params.arguments in place (pop), so the inner
    handler never sees __intent_id — keeping it out of per-tool argument
    schemas. Non-string or empty values are dropped silently.
    """

    async def wrapped(req):
        intent_id: str | None = None
        context_tokens: int | None = None
        args = getattr(req.params, "arguments", None)
        if isinstance(args, dict):
            if INTENT_ID_KWARG in args:
                candidate = args.pop(INTENT_ID_KWARG)
                if isinstance(candidate, str) and candidate:
                    intent_id = candidate
            if CONTEXT_TOKENS_KWARG in args:
                context_tokens = _coerce_context_tokens(args.pop(CONTEXT_TOKENS_KWARG))
        token = _set_intent_id(intent_id)
        ctx_token = _context_tokens_ctx.set(context_tokens)
        try:
            return await orig_handler(req)
        finally:
            _context_tokens_ctx.reset(ctx_token)
            _reset_intent_id(token)

    return wrapped
