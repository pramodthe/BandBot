#!/usr/bin/env bash
# One container runs the API and the worker so they share one database.
# DOCUMENTS_PATH=database stores uploads in that database. Any other value is a local directory.
set -euo pipefail

if [ "${DOCUMENTS_PATH:-}" != "database" ]; then
  mkdir -p "${DOCUMENTS_PATH:-/var/data/documents}"
fi
cd /app/backend

python -m app.worker &
worker_pid=$!
uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}" &
api_pid=$!

shutdown() {
  kill "$worker_pid" "$api_pid" 2>/dev/null || true
  wait "$worker_pid" "$api_pid" 2>/dev/null || true
}
trap shutdown TERM INT

wait -n "$worker_pid" "$api_pid"
shutdown
exit 1
