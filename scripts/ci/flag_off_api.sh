#!/usr/bin/env bash
# Start or stop the flag-off API for `make e2e` (S22 T18; C2): `start PORT` runs a second local
# API with FLIPKART_ENABLED=false on 127.0.0.1:PORT (uvicorn's loopback default; never a host
# flag) in the background, with a PID file, and waits up to 60 s for /readyz; `stop PORT` ends it.
# It shares the caller's env (the same .env, database and JWT secret as `make dev`), so the e2e
# can show the refusals of a deployment that keeps Flipkart off. Local dev tooling only.
set -euo pipefail
cd "$(dirname "$0")/../.."

mode="${1:-}"
port="${2:-}"
case "$port" in
  [1-9]|[1-9][0-9]|[1-9][0-9][0-9]|[1-9][0-9][0-9][0-9]|[1-9][0-9][0-9][0-9][0-9]) ;;
  *) echo "usage: flag_off_api.sh start|stop PORT" >&2; exit 2 ;;
esac
pidfile="${TMPDIR:-/tmp}/mkos-flag-off-api-${port}.pid"
logfile="${TMPDIR:-/tmp}/mkos-flag-off-api-${port}.log"

stop() {
  if [ -f "$pidfile" ]; then
    pid="$(cat "$pidfile")"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 100); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
    rm -f "$pidfile"
  fi
}

case "$mode" in
  start)
    stop
    FLIPKART_ENABLED=false nohup uv run --frozen python -m uvicorn app.main:app \
      --port "$port" --app-dir apps/api >"$logfile" 2>&1 &
    echo $! >"$pidfile"
    for _ in $(seq 1 60); do
      if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:${port}/readyz"; then
        exit 0
      fi
      sleep 1
    done
    echo "flag_off_api: not ready on 127.0.0.1:${port} after 60 s" >&2
    stop
    exit 1
    ;;
  stop) stop ;;
  *) echo "usage: flag_off_api.sh start|stop PORT" >&2; exit 2 ;;
esac
