#!/bin/sh
# APP_ROLE is the only knob. Cloud Run sets $PORT; everything execs so PID 1
# receives SIGTERM directly and in-flight work drains instead of being killed.
set -eu

ROLE="${APP_ROLE:-api}"
PORT="${PORT:-8080}"

case "$ROLE" in
  api)
    exec gunicorn app.main:app \
      --worker-class uvicorn.workers.UvicornWorker \
      --bind "0.0.0.0:${PORT}" \
      --workers "${WEB_CONCURRENCY:-2}" \
      --timeout "${GUNICORN_TIMEOUT:-120}" \
      --graceful-timeout 30 \
      --keep-alive 65 \
      --access-logfile - --error-logfile - --log-level "${LOG_LEVEL:-info}"
    ;;
  worker)
    # NOT bare `arq`: a Cloud Run service whose container never binds $PORT
    # fails its startup probe. app.workers.main runs arq + a health server.
    exec python -m app.workers.main
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  seed)
    exec python /src/scripts/seed.py --profile "${SEED_PROFILE:-demo}"
    ;;
  shell)
    exec python
    ;;
  *)
    echo "entrypoint: unknown APP_ROLE='${ROLE}' (api|worker|migrate|seed|shell)" >&2
    exit 64
    ;;
esac
