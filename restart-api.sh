#!/bin/bash
# Restarts the local uvicorn dev server on port 8000.
#
# Not using --reload: this app's APScheduler job (startup_sync) fires an
# immediate PDF fetch on every startup, and that in-flight job blocks
# WatchFiles' graceful shutdown on reload, leaving the server
# unreachable for an extended period. Restart manually with this script
# instead after any backend code change.

set -e
PORT=8000
LOG=/tmp/cosmic_lottery_oracle_v2_uvicorn.log
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

pid=$(lsof -ti :$PORT -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$pid" ]; then
  echo "Killing existing server (PID $pid)..."
  kill "$pid"
  sleep 1
fi

cd "$DIR"
.venv/bin/uvicorn api:app --host 127.0.0.1 --port $PORT > "$LOG" 2>&1 &
disown
sleep 2

if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/docs" | grep -q 200; then
  echo "Server restarted, healthy on port $PORT (log: $LOG)"
else
  echo "Server may not have started cleanly -- check $LOG"
  tail -20 "$LOG"
  exit 1
fi
