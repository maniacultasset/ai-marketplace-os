#!/usr/bin/env bash
# Image-contents check for the `bootstrap` job (AC11): non-root uid 10001, one uvicorn process for
# the api role, no gunicorn, no secrets, docs or local mock under /src, and /healthz answering.
# S3: the image serves the frozen /api/v1 surface (401 and 501 envelopes, every registry op).
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
     -o -path /src/docs -o -path /src/scripts/docgen -o -path /src/scripts/trace \
     -o -path /src/apps/api/app_mock')"
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

# S3: the frozen /api/v1 surface, through the image. No token is 401, and since S10 an unverified
# token is 401 too (no database is reachable here, and none is needed to refuse it); both are the
# error envelope with X-Request-ID; openapi.json lists every registry operation. A verified token
# reaching a 501 stub is the bootstrap auth leg's (scripts/ci/auth_e2e.py, on the compose network).
work="$(mktemp -d)"
trap 'cleanup; rm -rf "$work"' EXIT
api="http://127.0.0.1:${port}/api/v1/products"
code="$(curl -s -o "$work/401.json" -w '%{http_code}' "$api")"
[ "$code" = "401" ] || fail "GET /api/v1/products without a token gave $code, want 401"
code="$(curl -s -D "$work/bad.headers" -o "$work/bad.json" -w '%{http_code}' -H 'Authorization: Bearer x' "$api")"
[ "$code" = "401" ] || fail "GET /api/v1/products with an unverified token gave $code, want 401"
grep -qi '^x-request-id: ' "$work/bad.headers" || fail "the 401 envelope has no X-Request-ID"
python3 - "$work/401.json" "$work/bad.json" <<'PY' || fail "a /api/v1 error is not the envelope"
import json, sys
for path, code in zip(sys.argv[1:], ("UNAUTHENTICATED", "UNAUTHENTICATED")):
    error = json.load(open(path))["error"]
    assert error["type"] == code and error["error_report"]["error_code"] == code, error
    assert error["correlation_id"] and error["error_report"]["errors"][0]["source"] == "system", error
PY
registry="$(docker run --rm -e APP_ROLE=shell "$image" -c "from app.api.v1.surface import OPS; print(len(OPS))")"
curl -sf "http://127.0.0.1:${port}/openapi.json" >"$work/openapi.json" || fail "/openapi.json did not answer"
served="$(python3 -c 'import json, sys; spec = json.load(open(sys.argv[1])); print(sum(len(item) for path, item in spec["paths"].items() if path.startswith("/api/v1/")))' "$work/openapi.json")"
[ "$served" = "$registry" ] || fail "/openapi.json lists $served /api/v1 operations, the registry $registry"

echo "check_image: OK ($image: uid 10001, one uvicorn, no gunicorn, clean /src, /healthz 200, /api/v1 401 envelopes, $served operations)"
