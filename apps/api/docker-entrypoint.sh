#!/bin/sh
# APP_ROLE is the only knob. Cloud Run sets $PORT; every role execs, so PID 1 receives SIGTERM
# directly and in-flight work drains instead of being killed. `migrate` is the Cloud Run job's
# role (max-retries 0, never at container start); `seed` loads the local demo data and refuses
# any APP_ENV but local before connecting (S9). The worker role (S13) returns with its step.
set -eu

ROLE="${APP_ROLE:-api}"

case "$ROLE" in
  api)
    # One uvicorn process (ADR-0005). Cloud Run terminates TLS in front of the container, so the
    # forwarded headers are trusted and request.url reads https. The client IP is then the
    # left-most, client-supplied X-Forwarded-For entry: never key a rate limit on it. The app writes
    # its own access line (request id, route template, no query string), so uvicorn's is off.
    exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}" --proxy-headers --forwarded-allow-ips='*' --no-access-log
    ;;
  migrate)
    # alembic upgrade head as migrator, under the migration lock; one fixed output line.
    exec python -m app.cli db migrate
    ;;
  seed)
    exec python -m app.cli seed --profile "${SEED_PROFILE:-demo}"
    ;;
  shell)
    exec python "$@"
    ;;
  *)
    echo "entrypoint: unknown APP_ROLE='${ROLE}' (api|migrate|seed|shell)" >&2
    exit 64
    ;;
esac
