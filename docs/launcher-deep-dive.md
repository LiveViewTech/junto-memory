## 8A. The Launcher — What junto-launch.sh Does and Why

This section explains every step junto-launch.sh performs and why each step is necessary. Understanding this helps you diagnose failures and explains the purpose of each environment variable, flag, and config file the launcher touches.

### 8A.1 Why a Launcher Exists at All

Running plain `claude` gives you a Claude Code session with no junto context. The agent does not know its name, project, or what server to connect to. The launcher exists to solve three problems before Claude starts:

1. **Inject the system prompt** — claude's `--append-system-prompt-file` flag loads a file that is prepended to every conversation. This is how the agent learns its identity, operating rules, park checklist, and messaging behavior. Without it the agent has none of that.
2. **Set the environment variables** the plugin subprocess needs — the plugin runs as a separate process and reads config from the process environment, not from files. Those variables must be exported before `exec claude` replaces the shell.
3. **Enable the plugin channel** — the junto-inbox plugin is not on Anthropic's default channel allowlist. Loading it requires the `--dangerously-load-development-channels` flag, which the launcher passes automatically.

### 8A.2 Step-by-Step: What the Launcher Does

**Step 1 — Source the config file**
```bash
set -a
source ~/.junto/config
set +a
```
`set -a` marks all variables defined during `source` for automatic export. This means every variable in `~/.junto/config` (including `JUNTO_API_KEY`, `JUNTO_MEMORY_URL`, `JUNTO_ROLE`) becomes an environment variable inherited by all child processes — critically including the plugin subprocess. `set +a` stops auto-export after the source is done.

**Step 2 — Parse --no-plugin flag**
The launcher strips `--no-plugin` from its args before passing the remainder to claude. Everything else in `$@` is forwarded. This lets you run `junto --no-plugin` to launch without the inbox plugin when debugging plugin issues.

**Step 3 — Resolve agent identity**
The launcher tries four sources in priority order (see Section 5.1). Identity must be resolved before the system prompt can be rendered — the agent name, project, and component are all substituted into the template.

**Step 4 — Run the channel settings pre-flight**
```bash
bash ~/.claude/hooks/ensure-channel-settings.sh
```
This patches `~/.claude/remote-settings.json` to include `channelsEnabled: true` and the plugin in `allowedChannelPlugins`. It runs before claude starts because Claude Code reads the remote settings file very early in startup — if the settings aren't present before that read, the plugin is rejected before the `--dangerously-load-development-channels` flag even has a chance to work. Corporate MDM systems periodically overwrite this file; running the hook every launch ensures it is always current.

**Step 5 — Render the system prompt**
```bash
PROMPT_FILE=$(bash ~/.junto/templates/render.sh \
    --agent "$JUNTO_AGENT" \
    --project "$JUNTO_PROJECT" \
    --role "$JUNTO_ROLE" \
    --shared-memory-url "$JUNTO_MEMORY_URL" \
    --cwd "$(pwd)" \
    --api-key "$JUNTO_API_KEY" \
    --plugin-present true \
    --out "/tmp/junto-${JUNTO_AGENT}-${JUNTO_PROJECT}-prompt.md")
```
`render.sh` substitutes variables into `junto-system-prompt.md.tmpl` and writes the result to a temp file. The template contains the agent identity block, startup contract (`memory_start_session` instructions), go/park/status macro definitions, park checklist, and peer routing rules. Rendering it per-launch means every agent gets a fresh, correctly parameterized prompt — there is no shared prompt file that could carry stale identity from a previous session.

The `--api-key` flag embeds the key prefix in the auth block of the prompt as an informational hint ("Key prefix: smk_..."). The actual authentication uses the Bearer header in `~/.mcp.json` — the system prompt hint is just so the agent can reference which key it is using if needed.

The renderer fails loud if any `{{token}}` remains unresolved — it will exit non-zero before writing the file. This is why a missing required variable causes a silent exit when `set -euo pipefail` is active.

**Step 6 — Export identity and connection variables**
```bash
JUNTO_SHARED_MEMORY_URL="${JUNTO_MEMORY_URL}"
export JUNTO_AGENT JUNTO_PROJECT JUNTO_ROLE JUNTO_MEMORY_URL \
       JUNTO_SHARED_MEMORY_URL JUNTO_CHANNEL_DELAY JUNTO_COMPONENT
```
These must be explicitly exported so the plugin subprocess inherits them. The plugin reads:
- `JUNTO_SHARED_MEMORY_URL` — the server URL. The plugin reads this name specifically (not `JUNTO_MEMORY_URL`). The launcher bridges them by setting `JUNTO_SHARED_MEMORY_URL="${JUNTO_MEMORY_URL}"`. If you have a stale `JUNTO_SHARED_MEMORY_URL` in your `.bashrc` or `settings.json`, it will override this assignment because it was already in the environment when `exec claude` runs — this is the most common plugin misconfiguration.
- `JUNTO_AGENT` / `JUNTO_PROJECT` — the plugin calls `memory_start_session` with these to bind to the agent's inbox.
- `JUNTO_API_KEY` — the plugin passes this as `api_key` to `memory_start_session`. (Also exported by `set -a` during config sourcing.)
- `JUNTO_CHANNEL_DELAY` — milliseconds the plugin waits before its first `readInboxAndForward` call. Set to 15000 if messages arrive in the mailbox but not as live push — this gives Claude Code time to finish its channel approval handshake before the plugin tries to deliver. Most installs work at 0.
- `JUNTO_COMPONENT` — optional, passed to the plugin for component subscription (future pub/sub routing).

**Step 7 — Opt into the 1M context window**
```bash
export ANTHROPIC_DEFAULT_SONNET_MODEL="${ANTHROPIC_DEFAULT_SONNET_MODEL:-claude-sonnet-4-6[1m]}"
```
The 1M context window variant has the same per-token cost as the 200K variant but significantly reduces compaction frequency. The launcher sets this default; you can override it by exporting `ANTHROPIC_DEFAULT_SONNET_MODEL` before running junto.

**Step 8 — Point CC at the stable remote-settings file**
```bash
export CLAUDE_CODE_REMOTE_SETTINGS_PATH="${HOME}/.claude/managed-remote-settings.json"
```
Claude Code downloads org-policy settings from Anthropic and caches them in `~/.claude/remote-settings.json`. This download can overwrite `channelsEnabled` and the plugin allowlist. Setting `CLAUDE_CODE_REMOTE_SETTINGS_PATH` before CC starts redirects CC's policy cache to `~/.claude/managed-remote-settings.json` — a file we control and that the MDM agent does not touch. This env var must be set **before** `exec claude` because CC reads it at startup, before the settings.json env block is processed.

**Step 9 — exec claude**
```bash
exec claude \
    --append-system-prompt-file "$PROMPT_FILE" \
    --dangerously-load-development-channels "plugin:junto-inbox@tlemmons-junto-inbox"
```
`exec` replaces the bash process with claude — the launcher shell disappears and claude takes its process slot, inheriting all exported variables.

- `--append-system-prompt-file` — injects the rendered system prompt. This is what makes the agent junto-aware.
- `--dangerously-load-development-channels` — loads the junto-inbox plugin. The word "dangerously" is Anthropic's marker that this plugin is not on their vetted marketplace allowlist. For plugins on the official list, you would use `--channels` instead. For junto-inbox, this flag is the only option until Anthropic adds junto-inbox to their allowlist. On installs where the plugin allowlist is managed via `managed-remote-settings.json`, the flag bypasses the allowlist check rather than prompting the user each time.

### 8A.3 Why `set -euo pipefail` Matters

The launcher uses strict error handling (`set -euo pipefail`):
- `set -e` — exit immediately if any command returns non-zero. This is why a function ending with a failed `[[ ... ]]` expression will kill the script silently. The most common manifestation: `_junto_read_claude_md` ends with a component-check expression that returns 1 when no component is found, killing the script with no output.
- `set -u` — treat unset variables as errors. Prevents silent use of empty variables.
- `set -o pipefail` — a pipeline fails if any command in it fails.

Every function called before `exec claude` must return 0 (or be called in a context that handles non-zero returns). The functions `_junto_find_claude_md`, `_junto_read_agent_name_file`, and `_junto_read_claude_md` all need explicit `return 0` at their ends because their last statements are conditional expressions that may evaluate to false.

### 8A.4 The Config File (~/.junto/config)

```bash
# Required
JUNTO_API_KEY="smk_..."        # Bearer auth key for the server
JUNTO_MEMORY_URL="http://..."  # MCP server URL

# Optional with defaults
JUNTO_ROLE="General work agent"  # One-line description injected into system prompt
JUNTO_CHANNEL_DELAY=0            # Milliseconds before first push delivery (set to 15000 if needed)

# DO NOT set these — set per-directory in CLAUDE.md instead
# JUNTO_AGENT=""     # Uncomment only for a hard override across all directories
# JUNTO_PROJECT=""   # Uncomment only for a hard override across all directories
```

`JUNTO_AGENT` and `JUNTO_PROJECT` are intentionally commented out. If set here, they override CLAUDE.md detection for every directory on the machine — you would always launch as the same agent regardless of where you run `junto`. The correct place for per-directory identity is CLAUDE.md.

### 8A.5 Why the Plugin Needs Its Own Environment Variables

The junto-inbox plugin runs as a separate `bun` process spawned by Claude Code — it is not part of the claude process. It has no access to Claude Code's internal state, no access to `~/.mcp.json`, and no access to the session that Claude Code creates with the junto server. The plugin must make its **own independent HTTP connection** to the junto-memory server, call its own `memory_start_session`, and maintain its own SSE subscription.

This is why the launcher exports `JUNTO_AGENT`, `JUNTO_PROJECT`, `JUNTO_API_KEY`, and `JUNTO_SHARED_MEMORY_URL` as process environment variables rather than only writing them to the system prompt or config files. Environment variables are the only communication channel from the launcher to the plugin subprocess.

The `JUNTO_SHARED_MEMORY_URL` bridge exists because the plugin was originally written to read `SHARED_MEMORY_URL` (prefixed to `JUNTO_SHARED_MEMORY_URL` by the plugin's `envVar()` function), while the config file uses `JUNTO_MEMORY_URL` for the same value. The launcher sets `JUNTO_SHARED_MEMORY_URL="${JUNTO_MEMORY_URL}"` to bridge these two names. If you override `JUNTO_SHARED_MEMORY_URL` in your shell environment (e.g., a stale `.bashrc` entry pointing at an old server), it will take precedence over the launcher's bridge assignment and the plugin will connect to the wrong server.

