# junto context-report (per-box reporter)

`junto-context-report.py` runs once a minute on every machine that hosts junto agents. It does two things:
- it pushes each local agent's context size to the server (`memory_report_context`), which feeds `memory_get_agent_status` and the idle-park 200K gate;
- it carries out idle-park auto-resets (`/clear` + `go`) for opted-in agents (design:idle-park-autoreset-v0).

## Context sources
For each local `claude` process with `JUNTO_AGENT` in its environment, the reporter reads two sources and reports the fresher one:
1. `<cwd>/.claude/context-metrics.json`, written by the statusline (`~/.claude/statusline.sh`). Claude Code only runs the statusline while a client renders the TUI, so this file goes stale on unattended agents.
2. The newest session transcript `~/.claude/projects/<slug>/*.jsonl`. The slug is the cwd with every non-alphanumeric character replaced by `-`. The reporter takes the last assistant entry's usage (input + cache read + cache creation + output, the same sum the statusline reports). It reads only the tail of the file.

`--dry-run` prints both sources and the chosen value per agent, without calling the server.

Limitation: agents that share a cwd share one transcript directory. The newest transcript is attributed to every one of them.

## Install checklist (new box or container)
- `tmux`; the agent runs inside tmux, so the auto-reset can type into its pane.
- The agent's `claude` env carries `JUNTO_AGENT` and `JUNTO_PROJECT` (the junto launcher sets them).
- Statusline: `jq` if your statusline uses it (the sage one is sed-only). `mkdir -p <cwd>/.claude`.
- Python with the MCP SDK **pinned to `mcp==1.30.0`**: 2.x renamed `streamablehttp_client` and the reporter exits (learning_6126ed75e9ab0d29). Use a venv on minimal boxes.
- Copy the script to `~/.claude/junto-context-report.py`. Install `systemd/junto-context-report.{service,timer}` into `~/.config/systemd/user/` and add `Environment=JUNTO_SHARED_MEMORY_URL=http://<server>:8080/mcp` (keep `TimeoutStartSec=45`).
- `systemctl --user daemon-reload && systemctl --user enable --now junto-context-report.timer`; `loginctl enable-linger <user>`.
- Turn off the Claude Code feedback survey.
- Relaunch Claude Code once so it picks up the statusline config. Then check `memory_get_agent_status` for the agent's `context_tokens`.
