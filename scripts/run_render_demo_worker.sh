#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${AC_API_URL:?Set AC_API_URL to the public Render URL}"
: "${AC_NODE_KEY:?Set AC_NODE_KEY from the one-time node credentials screen}"
: "${AC_NODE_SECRET:?Set AC_NODE_SECRET from the one-time node credentials screen}"

PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || { echo "Create .venv and install backend runtime deps first." >&2; exit 1; }

cd "$ROOT/backend"
# DemoNodeWorker runs only built-in allow-listed handlers; it never runs agent
# supplied Python or shell code. It can run on a normal Render background worker.
exec "$PYTHON_BIN" -m app.worker.demo_node_worker \
  --api-url "$AC_API_URL" \
  --poll-interval 1 \
  --max-tasks 4
