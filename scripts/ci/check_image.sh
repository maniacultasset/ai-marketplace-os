#!/usr/bin/env bash
# Image-contents check for the `bootstrap` job (AC11): non-root uid 10001, one uvicorn process for
# the api role, no gunicorn, no secrets or docs under /src, and /healthz answering.
# Usage: scripts/ci/check_image.sh <image>
set -euo pipefail

image="${1:?usage: check_image.sh <image>}"
fail() { echo "check_image: $*" >&2; exit 1; }

uid="$(docker run --rm --entrypoint id "$image" -u)"
[ "$uid" = "10001" ] || fail "runs as uid $uid, want 10001"

if docker run --rm -e APP_ROLE=shell "$image" -c "import gunicorn" 2>/dev/null; then
  fail "gunicorn is importable"
fi

leaks="$(docker run --rm --entrypoint sh "$image" -c \
  'find /src \( -name ".env*" ! -name ".env.example" \) -o -name "*.pem" -o -name "*.key" \
     -o -path /src/docs -o -path /src/scripts/docgen -o -path /src/scripts/trace')"
[ -z "$leaks" ] || fail "unexpected files in the image: $leaks"

cid="$(docker run -d -p 127.0.0.1::8080 \
  -e APP_ENV=local \
  -e DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os \
  -e JWT_SECRET=ci_only_not_a_real_secret_at_least_32_chars \
  "$image")"
cleanup() { docker rm -f "$cid" >/dev/null; }
trap cleanup EXIT

port="$(docker port "$cid" 8080/tcp | head -n 1 | sed 's/.*://')"
healthy=0
for _ in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:${port}/healthz" >/dev/null; then healthy=1; break; fi
  sleep 1
done
[ "$healthy" -eq 1 ] || { docker logs "$cid" >&2; fail "/healthz did not answer"; }

pid1="$(docker exec "$cid" sh -c 'tr "\0" " " < /proc/1/cmdline')"
case "$pid1" in
  *"/uvicorn app.main:app"*) ;;
  *) fail "PID 1 is not uvicorn: $pid1" ;;
esac
pythons="$(docker exec "$cid" sh -c \
  'for f in /proc/[0-9]*/cmdline; do tr "\0" " " < "$f" 2>/dev/null; echo; done' | grep -c python)"
[ "$pythons" -eq 1 ] || fail "want exactly one python process, found $pythons"

echo "check_image: OK ($image: uid 10001, one uvicorn, no gunicorn, clean /src, /healthz 200)"
