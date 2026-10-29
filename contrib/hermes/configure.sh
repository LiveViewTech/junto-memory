#!/bin/sh
# Point Hermes at a local OpenAI-compatible model and at Junto over MCP.
# Env overrides: MODEL (default gpt-oss:20b), LLM_URL, JUNTO_URL.
set -e
C="$HOME/.hermes/config.yaml"
MODEL="${MODEL:-gpt-oss:20b}"
LLM_URL="${LLM_URL:-http://localhost:11434/v1}"
JUNTO_URL="${JUNTO_URL:-http://localhost:8080}"
sed -i "s#^  default: \".*\"#  default: \"$MODEL\"#; s#^  provider: \"auto\"#  provider: \"custom\"#; \
s#^  base_url: \"https://openrouter.ai/api/v1\"#  base_url: \"$LLM_URL\"#; \
s#^  adopt_external_logins: true#  adopt_external_logins: false#" "$C"
grep -q '^mcp_servers:' "$C" || cat >> "$C" <<YAML

mcp_servers:
  junto:                  # 13-tool discussion profile; takes junto_session
    url: "$JUNTO_URL/mcp/discussion"
  junto_full:             # every tool (backlog etc.); takes session_id
    url: "$JUNTO_URL/mcp"
YAML
echo "configured: model=$MODEL llm=$LLM_URL junto=$JUNTO_URL"
