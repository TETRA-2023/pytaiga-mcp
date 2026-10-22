#!/bin/bash
# Open the MCP Inspector on this server (stdio).
#   ./inspect.sh                 # uses TAIGA_* from the environment / .env
# Env: TAIGA_API_URL, TAIGA_USERNAME, TAIGA_PASSWORD (auto-auth), or call login() in the UI.
set -euo pipefail
cd "$(dirname "$0")"
exec npx -y @modelcontextprotocol/inspector uv --directory "$PWD" run mcp-taiga "$@"
