#!/usr/bin/env bash
# Starts the fake AgentMail API and imapgw, runs scripts/smoke.py, and tears both down.
# Exit code is the smoke test's exit code.
set -euo pipefail
cd "$(dirname "$0")/.."

export AGENTMAIL_API_URL="${AGENTMAIL_API_URL:-http://127.0.0.1:3210/v0}"
export AGENTMAIL_INBOX_ID="${AGENTMAIL_INBOX_ID:-candidate@imap.test}"
export AGENTMAIL_API_KEY="${AGENTMAIL_API_KEY:-test_agentmail_key}"
export IMAP_HOST="${IMAP_HOST:-127.0.0.1}"
export IMAP_PORT="${IMAP_PORT:-1143}"
export IMAPGW_DB_PATH="${IMAPGW_DB_PATH:-$(mktemp -d)/imapgw-smoke.sqlite3}"
export IMAPGW_LOG_LEVEL="${IMAPGW_LOG_LEVEL:-WARNING}"

PIDS=()
cleanup() {
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT

wait_port() {
  local host="$1" port="$2" tries=50
  until uv run python -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(0 if s.connect_ex(('$host', $port))==0 else 1)"; do
    tries=$((tries-1)); [ "$tries" -gt 0 ] || { echo "timeout waiting for $host:$port"; exit 1; }
    sleep 0.2
  done
}

if [ "${SKIP_FAKE_API:-0}" != "1" ]; then
  HARNESS_QUIET=1 npm --prefix test-harness run api >/dev/null 2>&1 &
  PIDS+=($!)
  wait_port 127.0.0.1 3210
fi

uv run python -m imapgw >/dev/null 2>&1 &
PIDS+=($!)
wait_port "$IMAP_HOST" "$IMAP_PORT"

uv run python scripts/smoke.py
