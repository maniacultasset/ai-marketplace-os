#!/bin/sh
# APP_ROLE is the only knob. Cloud Run sets $PORT; every role execs, so PID 1 receives SIGTERM
# directly and in-flight work drains instead of being killed. `migrate` is the Cloud Run job's
# role (max-retries 0, never at container start); `seed` loads the local demo data and refuses
# any APP_ENV but local before connecting (S9). `worker` runs both queues in one process with no
# port and no health server (a Cloud Run worker pool needs no $PORT; CANON.md row 25).
set -eu

ROLE="${APP_ROLE:-api}"

case "$ROLE" in
  api)
    # One uvicorn process (ADR-0005). Cloud Run terminates TLS in front of the container, so the
    # forwarded headers are trusted and request.url reads https. The client IP is then the
    # left-most, client-supplied X-Forwarded-For entry: never key a rate limit on it. The app writes
    # its own access line (request id, route template, no query string), so uvicorn's is off.
    exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}" --proxy-headers --forwarded-allow-ips='*' --no-access-log --no-server-header
    ;;
  worker)
    # procrastinate on queues default and publish (S13); SIGTERM drains within 10 s.
    export APP_NAME="${APP_NAME:-marketplace-os-worker}"
    export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-marketplace-os-worker}"
    exec python -m app.workers.main
    ;;
  migrate)
    # alembic upgrade head as migrator, under the migration lock, then the attribute dictionary
    # sync the facts guard reads (DATABASE.md §8.2); one fixed output line each.
    python -m app.cli db migrate
    exec python -m app.cli dictionary sync
    ;;
  seed)
    exec python -m app.cli seed --profile "${SEED_PROFILE:-demo}"
    ;;
  shell)
    exec python "$@"
    ;;
  *)
    echo "entrypoint: unknown APP_ROLE='${ROLE}' (api|worker|migrate|seed|shell)" >&2
    exit 64
    ;;
esac
