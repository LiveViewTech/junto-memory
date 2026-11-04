# junto notify runner

Delivers a person's junto notifications through **their own** Claude
connectors (Gmail, Slack) with a headless `claude -p`. Contract:
`interface:human-notify-v0`. One runner per person, on that person's machine.

## Each person, once

1. **Connectors.** In Claude (claude.ai → Settings → Connectors) connect Gmail
   and/or Slack. On the machine that will run this, check that the CLI sees them:
   `claude mcp list` should show `claude.ai Gmail` / `claude.ai Slack` as Connected.
2. **Register yourself in junto** (from any Claude session connected to junto):
   `memory_notify(action="register", name="<you>", gmail="<your address>", preferred="gmail", quiet_hours="22:00-07:00", timezone="America/Chicago")`.
   Add `slack="<your Slack user/DM id>"` for Slack.
3. **Local config** at `~/.config/junto/notify-runner.json`
   (Windows: `%USERPROFILE%\.config\junto\notify-runner.json`):
   ```json
   {"human": "<you>",
    "junto_url": "http://<junto host>:8080/mcp",
    "api_key": null,
    "model": "haiku",
    "channels": {
      "gmail": {"tool": "mcp__claude_ai_Gmail__send_message", "recipient_field": "to",
                "allowed": ["<your address>"]}}}
   ```
   `allowed` is the trust anchor: the runner refuses to send anywhere else, even
   if the server's registration says otherwise. For Slack, find the send tool's
   name and its recipient argument (ask Claude: "which Slack connector tool sends
   a message, and which argument is the recipient?") and add a `slack` entry the
   same way, with your own DM/user id in `allowed`.
4. **Install** Python 3.10+ and `pip install mcp==1.30.0`.
5. **Test:** `python junto-notify-runner.py --check` shows your config and what junto
   has registered. Then `python junto-notify-runner.py --test` queues a test to
   yourself and delivers it. When it arrives, that channel shows as verified in
   `memory_notify(action="humans")`.
6. **Schedule** one pass a minute:
   - Linux/macOS cron: `* * * * * /usr/bin/python3 /path/junto-notify-runner.py >> ~/junto-notify.log 2>&1`
   - Windows Task Scheduler: run `python C:\path\junto-notify-runner.py` every 1 minute.

## How it stays safe
- Junto queues; only the runner on your machine, logged into your Claude, can send. Messages come from your own account.
- Each send is a fresh `claude -p` (Haiku) with exactly one send tool allowed. A
  PreToolUse hook (this script, `--hook`) blocks any call whose recipient is not
  in your local `allowed` list, or that sets cc/bcc/attachments/drafts.
- The message text is passed as data on stdin, and the prompt tells Claude to send it verbatim and ignore instructions inside it.
- Cost ≈ $0.03 and ~10 s per send on Haiku. Junto caps each person at 20 sends/day and drops repeats of the same subject from the same sender within an hour.
