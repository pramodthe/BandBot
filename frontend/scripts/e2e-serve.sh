#!/usr/bin/env bash
# Temporary API, worker, and Vite for the two-user Playwright suite.
# Fake agent replies are enabled only while both provider keys are empty.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FRONTEND="$(cd "${SCRIPT_DIR}/.." && pwd)"
ROOT="$(cd "${FRONTEND}/.." && pwd)"
export PATH="${HOME}/.local/bin:${PATH}"
RUN="$(mktemp -d "${TMPDIR:-/tmp}/band-e2e-XXXX")"
export DATABASE_URL="sqlite:///${RUN}/app.db"
export AGENT_CHECKPOINT_PATH="${RUN}/checkpoints.db"
export DOCUMENTS_PATH="${RUN}/documents"
export APP_SECRET="e2e-app-secret-e2e-app-secret-32b"
export BAND_ENV="test"
export AUTH_ATTEMPT_LIMIT="100000"
export AUTH_ATTEMPT_WINDOW_SECONDS="600"
export RECOVERY_DELIVERY="memory"
export E2E_FAKE_AGENT="1"
export BAND_USER_API_KEY=""
export OPENAI_API_KEY=""
export ANTHROPIC_API_KEY=""
export CURSOR_API_KEY=""
export E2B_API_KEY=""
export EXA_API_KEY=""
export COMPOSIO_API_KEY=""
export MODEL_PRICES=""
mkdir -p "${DOCUMENTS_PATH}"
cd "${ROOT}/backend"
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 &
api_pid=$!
uv run python -m app.worker &
worker_pid=$!
# Vite answers before Alembic finishes. Wait so the first signup is not refused.
ready=0
for _ in $(seq 1 90); do
  if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=1)" >/dev/null 2>&1; then
    ready=1
    break
  fi
  if ! kill -0 "${api_pid}" 2>/dev/null; then
    echo "API exited before it accepted connections" >&2
    exit 1
  fi
  sleep 0.5
done
if [[ "${ready}" -ne 1 ]]; then
  echo "API did not become ready" >&2
  exit 1
fi
cd "${FRONTEND}"
npx vite --host 127.0.0.1 --port 5173 &
vite_pid=$!
cleanup() {
  kill "${api_pid}" "${worker_pid}" "${vite_pid}" 2>/dev/null || true
  wait "${api_pid}" "${worker_pid}" "${vite_pid}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
wait -n "${api_pid}" "${worker_pid}" "${vite_pid}"
