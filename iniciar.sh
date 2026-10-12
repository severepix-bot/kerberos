#!/bin/sh
set -eu

cd "$(dirname "$0")"
export CEREBRO_SERVER_URL="${CEREBRO_SERVER_URL:-https://cerebrocentra.com}"
if ! command -v npm >/dev/null 2>&1; then
  echo "Node.js/npm é necessário para instalar os plugins MCP. Instale em https://nodejs.org/" >&2
  exit 1
fi
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --quiet -r requirements.txt
if [ ! -f .plugins/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js ] \
  || [ ! -f .plugins/node_modules/@modelcontextprotocol/server-memory/dist/index.js ] \
  || [ ! -f .plugins/node_modules/next-devtools-mcp/dist/index.js ] \
  || [ ! -f .plugins/node_modules/@modelcontextprotocol/server-github/dist/index.js ] \
  || [ ! -f .plugins/node_modules/@googleworkspace/cli/run.js ] \
  || [ ! -f .plugins/node_modules/@microsoft/workiq/bin/workiq.js ] \
  || [ ! -f .plugins/node_modules/apple-photos-mcp/build/index.js ]; then
  npm install --prefix .plugins --no-save --package-lock=false \
    @modelcontextprotocol/server-filesystem@2026.8.31 \
    @modelcontextprotocol/server-memory@2026.8.31 \
    next-devtools-mcp@0.4.0 \
    @modelcontextprotocol/server-github@2025.4.8 \
    @googleworkspace/cli@0.22.5 \
    @microsoft/workiq@1.0.1 \
    apple-photos-mcp@2.1.20
fi
.venv/bin/python -m playwright install chromium
exec .venv/bin/python -m assistente "$@"
