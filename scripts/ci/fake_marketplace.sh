#!/usr/bin/env bash
# Start or stop the local fake marketplace for `make e2e` (S15): `start PORT` runs it in the
# background on 127.0.0.1:PORT (the module binds loopback) with a PID file and waits up to 60 s
# for /healthz; `stop PORT` ends it. Local dev tooling only; never part of the image.
set -euo pipefail
cd "$(dirname "$0")/../.."

mode="${1:-}"
port="${2:-}"
case "$port" in
  [1-9]|[1-9][0-9]|[1-9][0-9][0-9]|[1-9][0-9][0-9][0-9]|[1-9][0-9][0-9][0-9][0-9]) ;;
  *) echo "usage: fake_marketplace.sh start|stop PORT" >&2; exit 2 ;;
esac
pidfile="${TMPDIR:-/tmp}/mkos-fake-marketplace-${port}.pid"
logfile="${TMPDIR:-/tmp}/mkos-fake-marketplace-${port}.log"

stop() {
  if [ -f "$pidfile" ]; then
    pid="$(cat "$pidfile")"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 50); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
    rm -f "$pidfile"
  fi
}

case "$mode" in
  start)
    stop
    nohup uv run --frozen python apps/api/tests/support/fake_marketplace.py --port "$port" \
      >"$logfile" 2>&1 &
    echo $! >"$pidfile"
    for _ in $(seq 1 60); do
      if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:${port}/healthz"; then
        exit 0
      fi
      sleep 1
    done
    echo "fake_marketplace: not healthy on 127.0.0.1:${port} after 60 s" >&2
    stop
    exit 1
    ;;
  stop) stop ;;
  *) echo "usage: fake_marketplace.sh start|stop PORT" >&2; exit 2 ;;
esac
