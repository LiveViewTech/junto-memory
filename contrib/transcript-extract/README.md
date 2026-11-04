# Transcript extraction (Memento #4)

Nightly job: reads new Claude Code transcripts for opted-in project dirs and
writes decision / explanation / resolution records to junto as **unreviewed**
learnings with provenance. Contract: `design:transcript-extraction-v0`.

Requires a server with secret redaction (interface:secret-redaction-v0) and
verification state (interface:verification-state-v0), i.e. 1.42.0 or newer.

## Opt in

`~/.config/junto/transcript-extract.json`:

```json
{"projects": {"-home-tlemmons-HA": "ha"}, "model": "sonnet", "max_records_per_chunk": 5}
```

Keys are directory names under `~/.claude/projects/`; values are junto
projects. No file, or an empty map, makes every run a no-op.

## Server switch (1.43.1+)

The server must also allow it: query_config `transcript_extract`, **off by
default**. An operator turns it on for the whole server, or for one project:

```python
memory_admin(action="query_set_config", key="transcript_extract", value=True)                     # server-wide
memory_admin(action="query_set_config", key="transcript_extract", value=False, project="nimbus")  # one project off
```

Each run first reads `memory_health` → `features.transcript_extract` (the
server default). If it is off, the run exits **before any model call**, so a
disabled server costs nothing. A per-project override is enforced at write
time: `memory_record_learning` rejects `transcript-extract`-tagged writes
with `code: transcript_extract_disabled`, and the run skips the rest of that
project. In that case the model call has already been paid for. A server
older than 1.43.1 has no switch; the run warns and proceeds. An unreachable
server makes the run exit 2.

## Run

```bash
python3 junto-transcript-extract.py --dry-run --limit 1   # print records, write nothing
python3 junto-transcript-extract.py                        # extract + write
```

Cron (after the librarian, so its facets pass sees fresh rows next night):

```
10 3 * * * cd ~/sharedUtils/junto/junto-memory && .venv/bin/python contrib/transcript-extract/junto-transcript-extract.py >> ~/junto-logs/transcript-extract.log 2>&1
```

## What it does

- Skips sessions touched in the last 2 hours and headless `claude -p` runs
  (`entrypoint: sdk-cli`, which covers the librarian).
- Keeps only user and assistant text, plus a one-line hint per tool call.
  Drops tool results, thinking, sidechains and system reminders. Redacts
  secrets before the model sees anything.
- Caps each session at 3 chunks of 60K chars (newest wins). Watermarks per
  transcript in `~/.local/state/junto-transcript-extract/state.json`, so a
  long session is read once.
- Calls `claude -p` with no tools, no MCP and no saved session. It runs on
  the logged-in subscription; set `ANTHROPIC_API_KEY` to bill the API instead.
- Writes as `extractor@junto` via MCP, so server redaction, the write-time
  gate and facets all apply. Skips a record when the project's top
  memory_query hit is at least 85% similar. Provenance (kind, CC session id,
  line span, transcript path) is in a footer and in tags
  `transcript-extract`, `kind:<k>`, `cc-session:<id>`.

## Windows (Claude Code on a Windows workstation)

The script is cross-platform. On the Windows box:

1. Python 3.11+, `pip install mcp==1.30.0`, and `claude` on PATH (logged in).
2. A checkout of this repo (the script imports the redactor from `src/`):
   `git clone https://github.com/tlemmons/junto-memory C:\code\junto-memory`
3. `%USERPROFILE%\.config\junto\transcript-extract.json`, keyed by the folder
   names under `%USERPROFILE%\.claude\projects\` (e.g. `C--code-Nimbus`,
   `C--code-Nimbus-server-team`). Each value is the junto project:
   ```json
   {"projects": {"C--code-Nimbus": "nimbus"}, "model": "sonnet"}
   ```
4. Point it at the server: set `JUNTO_MCP_URL=http://<junto host>:8080/mcp`.
   Set `JUNTO_API_KEY` if the server needs a key from that host.
5. Task Scheduler, daily 03:10:
   `python C:\code\junto-memory\contrib\transcript-extract\junto-transcript-extract.py`
   Run `--dry-run --limit 1` once by hand first.
