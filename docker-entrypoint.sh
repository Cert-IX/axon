#!/bin/bash
# Axon MCP Server Docker Entrypoint
# 1. Indexes the mounted repository if no index exists
# 2. Starts the axon host with MCP + UI + file watching
# 3. Handles graceful shutdown on SIGTERM/SIGINT

set -euo pipefail

# Forward signals to the axon host process for graceful shutdown
trap 'kill -TERM $PID 2>/dev/null; wait $PID' SIGTERM SIGINT

REPO_DIR="${AXON_REPO_DIR:-/repo}"
PORT="${AXON_PORT:-8420}"
BIND="${AXON_BIND:-0.0.0.0}"
WATCH="${AXON_WATCH:-true}"
NO_EMBEDDINGS="${AXON_NO_EMBEDDINGS:-true}"

echo "=== Axon MCP Server ==="
echo "  Repo:  ${REPO_DIR}"
echo "  Port:  ${PORT}"
echo "  Watch: ${WATCH}"

cd "${REPO_DIR}"

# Index the repo if no meta.json exists (first run or after clean)
if [ ! -f "${REPO_DIR}/.axon/meta.json" ]; then
    echo ""
    echo "=== Indexing repository (first run) ==="
    EMBED_FLAG=""
    if [ "${NO_EMBEDDINGS}" = "true" ]; then
        EMBED_FLAG="--no-embeddings"
    fi
    axon analyze . ${EMBED_FLAG}
    echo "=== Indexing complete ==="
else
    echo "  Index found: $(python3 -c "
import json, sys
try:
    m = json.load(open('${REPO_DIR}/.axon/meta.json'))
    s = m.get('stats', {})
    print(f\"{s.get('symbols','?')} symbols, {s.get('relationships','?')} rels, {s.get('dead_code','?')} dead code\")
except: print('(unable to read meta)')
")"
fi

echo ""
echo "=== Starting Axon Host ==="
echo "  UI:  http://${BIND}:${PORT}"
echo "  MCP: http://${BIND}:${PORT}/mcp"

# Build the axon host command
HOST_ARGS="host --port ${PORT} --bind ${BIND} --no-open"

if [ "${WATCH}" = "true" ]; then
    HOST_ARGS="${HOST_ARGS} --watch"
else
    HOST_ARGS="${HOST_ARGS} --no-watch"
fi

# Start axon host in the repo directory, exec replaces shell for signal handling
exec axon ${HOST_ARGS}
