"""Write-time secret redaction (Memento #3, backlog_0ef9d63fbe9b).

Best-effort: agent-authored text is scanned for credentials BEFORE it reaches
Mongo/Chroma, and each hit is replaced in place with ``[REDACTED:<kind>]``.
The caller gets the redacted text plus a per-kind count to surface in the tool
response, so the writer learns a secret was dropped (and never sees it echoed).

Two classes of detector:

1. SHAPED tokens: vendor prefixes and fixed structures (PEM blocks, AKIA…,
   ghp_…, sk-ant-…, smk_…, JWTs). These are near-zero false-positive, so they
   redact unconditionally.
2. GENERIC assignments: ``password=…``, ``"api_key": "…"``, ``token: …``,
   ``Authorization: Bearer …``, ``scheme://user:PASS@host`` and
   ``user @ host:port : PASS``. Prose like
   "the password is in 1Password" must survive, so the value has to look like
   a credential: no whitespace, at least 8 chars, not a placeholder
   (``<…>``, ``${…}``, ``$VAR``, ``xxxx``, ``***``, ``changeme``…), and
   Shannon entropy over the threshold. Only the VALUE is replaced; the key
   name stays so the text still reads.

Never rejects a write. Redaction is lossy by design: the secret is gone from
the store, which is the point. Over-redaction costs a token of context;
under-redaction leaks a credential to every agent on the server.
"""

import math
import re
from typing import Dict, List, Optional, Tuple

# kind -> compiled pattern. Order matters: PEM first (it spans lines and
# contains base64 that the shorter detectors would otherwise nibble at).
_SHAPED: List[Tuple[str, re.Pattern]] = [
    ("private_key", re.compile(
        r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----",
        re.DOTALL)),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{32,}")),
    ("junto_key", re.compile(r"\bsmk_[A-Za-z0-9_\-]{30,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{10,}")),
    # Base64 JSON blobs ({"... → eyJ) of credential length, e.g. a cloudflared
    # tunnel token seen in `ps` output. Shorter eyJ strings are left alone.
    ("encoded_token", re.compile(r"\beyJ[A-Za-z0-9+/_\-]{60,}={0,2}")),
]

# scheme://user:PASSWORD@host — only the password group is replaced.
_URL_CRED_RE = re.compile(
    r"(?P<pre>\b[a-zA-Z][a-zA-Z0-9+.\-]{1,20}://[^\s:/@]+:)(?P<val>[^\s@/]+)(?P<post>@)"
)

# CLI flags: --token <v>, --password=<v>, --api-key <v> ...
_CLI_FLAG_RE = re.compile(
    r"(?P<pre>(?<![\w-])--(?:token|password|passwd|secret|api-key|apikey|access-key|auth-token)(?:=|\s+))"
    r"(?P<val>[^\s\"'`]{8,})",
    re.IGNORECASE,
)

# Authorization: Bearer <tok> / Basic <tok>
_AUTH_HEADER_RE = re.compile(
    r"(?P<pre>\b(?:Bearer|Basic)\s+)(?P<val>[A-Za-z0-9_\-.=+/]{16,})"
)

# key <sep> value, with optional quotes. Key names are matched as a suffix so
# MONGO_PASSWORD, db_password, x-api-key, client_secret all hit.
_ASSIGN_RE = re.compile(
    r"(?P<pre>\b[A-Za-z0-9_.\-]*?"
    r"(?:password|passwd|pwd|pass|secret|api[_\-]?key|apikey|access[_\-]?key|auth[_\-]?token|token|credential)"
    r"[\"']?\s*(?:=|:|=>)\s*[\"']?)"
    r"(?P<val>[^\s\"'`,;&}{)\]]{8,})",
    re.IGNORECASE,
)

# user @ host:PORT : secret — a connection handle written as prose (infra-team
# 2026-11-03: two staging DB passwords got through in this shape). The port is
# required so plain agent addressing ("memory@junto: …") never matches.
_CONN_HANDLE_RE = re.compile(
    r"(?P<pre>\b[\w.\-]+\s*@\s*[\w.\-]+:\d{2,5}\s*:\s*)(?P<val>[^\s\"'`,;]{12,})"
)

_PLACEHOLDER_RE = re.compile(
    r"^(?:<.*>|\$?\{.*\}|\$[A-Za-z_][A-Za-z0-9_]*|%\(?[A-Za-z_]+\)?s?|\{\{.*\}\}|"
    r"[xX*•.\-_]+|\[REDACTED(?::[a-z_]+)?\]|"
    r"(?:changeme|pass|pw|password|secret|example|redacted|placeholder|your[_\-].*|none|null|true|false|undefined)\d*)$",
    re.IGNORECASE,
)

GENERIC_MIN_ENTROPY = 3.0


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    counts: Dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _looks_secret(val: str) -> bool:
    if _PLACEHOLDER_RE.match(val):
        return False
    # References, not values: a URL, a path, a <placeholder>, or an env-var
    # name (MONGO_PASSWORD, $(cat …)).
    if val[0] in "<$./~" or re.match(r"https?://", val, re.IGNORECASE):
        return False
    if re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+", val):
        return False
    # Identifiers and code references are not credentials: a dotted/called
    # name (os.environ.get, settings.API_KEY) or a pure lower_snake word.
    if "(" in val or "[" in val or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", val):
        return False
    if re.fullmatch(r"[a-z]+(?:[_\-][a-z]+)*", val):
        return False
    # CamelCase names (SystemConfig, CheckoutToken) and file paths
    # (analyst/secrets/ga4-key.json) name where a secret lives, not the secret.
    if re.fullmatch(r"[A-Z]?[a-z]+(?:[A-Z][a-z]+)+", val):
        return False
    if re.fullmatch(r"[\w.\-]+(?:/[\w.\-]+)+\.[A-Za-z]{1,5}", val):
        return False
    # Short human passwords (T0mTest99) sit below any entropy bar that
    # spares prose, so letters mixed with digits/symbols count on their own.
    if re.search(r"[A-Za-z]", val) and re.search(r"[^A-Za-z]", val):
        return True
    return shannon_entropy(val) >= GENERIC_MIN_ENTROPY


def _key_label(kind: str, pre: str) -> Optional[str]:
    """What the secret was attached to, never the secret: the assignment's key
    name (MONGO_PASSWORD), the URL scheme (mongodb), or the auth scheme
    (Bearer). Lets a reviewer judge a false positive without the value."""
    if kind == "url_password":
        return pre.split("://", 1)[0]
    m = re.search(r"([A-Za-z0-9_.\-]+)[\"']?\s*(?:=|:|=>)?\s*[\"']?\s*$", pre)
    return m.group(1)[-60:] if m else None


def redact_secrets(text: str, hits: Optional[List[Dict]] = None) -> Tuple[str, Dict[str, int]]:
    """Return (redacted_text, {kind: count}). Empty dict = nothing found.
    When `hits` is a list, one {kind, key, len} dict per redaction is appended
    to it (metrics, interface:secret-redaction-v0 §metrics)."""
    if not text or not isinstance(text, str):
        return text, {}
    found: Dict[str, int] = {}

    def _bump(kind: str, key: Optional[str] = None, length: int = 0) -> str:
        found[kind] = found.get(kind, 0) + 1
        if hits is not None:
            hits.append({"kind": kind, "key": key, "len": length})
        return f"[REDACTED:{kind}]"

    for kind, pat in _SHAPED:
        text = pat.sub(lambda m, k=kind: _bump(k, None, len(m.group(0))), text)

    def _value_sub(kind: str):
        def _f(m):
            val = m.group("val")
            if val.startswith("[REDACTED"):
                return m.group(0)
            if kind != "url_password" and not _looks_secret(val):
                return m.group(0)
            # A bare upper-case PASS is a test/review verdict ("Reviews PASS:
            # f1c17da5"), not a credential key; DB_PASS / pass: still hit.
            if kind == "assigned_secret" and _key_label(kind, m.group("pre")) == "PASS":
                return m.group(0)
            if kind == "url_password" and (len(val) < 6 or _PLACEHOLDER_RE.match(val)
                                           or val[0] in "<$"):
                return m.group(0)
            return (m.group("pre") + _bump(kind, _key_label(kind, m.group("pre")), len(val))
                    + (m.groupdict().get("post") or ""))
        return _f

    text = _URL_CRED_RE.sub(_value_sub("url_password"), text)
    text = _AUTH_HEADER_RE.sub(_value_sub("auth_header"), text)
    text = _CLI_FLAG_RE.sub(_value_sub("cli_secret"), text)
    text = _ASSIGN_RE.sub(_value_sub("assigned_secret"), text)
    text = _CONN_HANDLE_RE.sub(_value_sub("conn_secret"), text)
    return text, found


def redaction_notice(found: Dict[str, int]) -> Dict:
    """Response payload for a write that had secrets removed."""
    total = sum(found.values())
    return {
        "redacted": found,
        "redacted_warning": (
            f"{total} secret(s) were replaced with [REDACTED:<kind>] before storing. "
            "The original values were NOT persisted. Keep credentials in a secret "
            "store and reference them by name."
        ),
    }


def redact_fields(*values: str, tool: Optional[str] = None,
                  session_id: Optional[str] = None,
                  fields: Optional[List[str]] = None) -> Tuple[list, Dict[str, int]]:
    """Redact several free-text fields of one write. Returns the values in the
    same order (None passes through) plus the merged per-kind counts.

    With `tool`, a write that had anything removed is logged to
    redaction_events (log_redaction_event); `fields` names the values for
    that log."""
    out = []
    merged: Dict[str, int] = {}
    hits: List[Dict] = []
    for i, v in enumerate(values):
        before = len(hits)
        v2, found = redact_secrets(v, hits)
        for h in hits[before:]:
            h["field"] = fields[i] if fields and i < len(fields) else i
        out.append(v2)
        for k, n in found.items():
            merged[k] = merged.get(k, 0) + n
    if hits and tool:
        log_redaction_event(tool, session_id, hits)
    return out, merged


REDACTION_EVENTS = "redaction_events"
REDACTION_EVENTS_TTL_SECONDS = 365 * 24 * 3600
_events_indexed = False


def log_redaction_event(tool: str, session_id: Optional[str], hits: List[Dict]) -> None:
    """One row per write that had secrets removed: {ts, tool, project, agent,
    hits:[{kind, field, key, len}]}. Never the value. Best-effort; never
    fails a write."""
    global _events_indexed
    try:
        from shared_memory.clients import get_mongo
        from shared_memory.helpers import utc_now
        from shared_memory.state import active_sessions
        db = get_mongo()
        if db is None:
            return
        if not _events_indexed:
            db[REDACTION_EVENTS].create_index("ts", expireAfterSeconds=REDACTION_EVENTS_TTL_SECONDS)
            _events_indexed = True
        sess = active_sessions.get(session_id) or {}
        db[REDACTION_EVENTS].insert_one({
            "ts": utc_now(), "tool": tool,
            "project": sess.get("project"), "agent": sess.get("claude_instance"),
            "hits": hits,
        })
    except Exception:  # noqa: BLE001 - metrics are never load-bearing
        pass
