#!/bin/sh
# APP_ROLE is the only knob. Cloud Run sets $PORT; every role execs, so PID 1 receives SIGTERM
# directly and in-flight work drains instead of being killed. The worker (S13), migrate (S5) and
# seed (S9) roles return with their steps.
set -eu

ROLE="${APP_ROLE:-api}"

case "$ROLE" in
  api)
    # One uvicorn process (ADR-0005). Cloud Run terminates TLS in front of the container, so the
    # forwarded headers are trusted and request.url reads https. The client IP is then the
    # left-most, client-supplied X-Forwarded-For entry: never key a rate limit on it.
    exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}" --proxy-headers --forwarded-allow-ips='*'
    ;;
  shell)
    exec python "$@"
    ;;
  *)
    echo "entrypoint: unknown APP_ROLE='${ROLE}' (api|shell)" >&2
    exit 64
    ;;
esac
