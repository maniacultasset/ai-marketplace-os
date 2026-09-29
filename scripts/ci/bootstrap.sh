#!/usr/bin/env bash
# The `bootstrap` required check: CANON-ADDENDUM.md §7, reduced per ADR-0013 §4 (no alembic until
# S5, no seed until S9, and the image import omits mkos_pipeline until S14). The MinIO bucket
# check lands with the MinIO services. It never copies .env and never runs `docker compose down -v`.
set -euo pipefail
cd "$(dirname "$0")/../.."

docker compose up -d --wait
make bootstrap-db

uv run uvicorn app.main:app --port 8000 --app-dir apps/api &
server=$!
stop_server() { if kill -0 "$server" 2>/dev/null; then kill "$server"; fi; }
trap stop_server EXIT

for _ in $(seq 1 30); do
  if curl -sf localhost:8000/healthz >/dev/null; then break; fi
  sleep 2
done
curl -sf localhost:8000/healthz && curl -sf localhost:8000/readyz
echo

docker build -f apps/api/Dockerfile -t mkos-api:ci .
docker run --rm -e APP_ROLE=shell mkos-api:ci -c "import app, mkos_schemas"
scripts/ci/check_image.sh mkos-api:ci
