#!/usr/bin/env bash
# The `bootstrap` required check: CANON-ADDENDUM.md §7, reduced per ADR-0013 §4 (no alembic until
# S5, no seed until S9, and the image import omits mkos_pipeline until S14).
# It never copies .env and never runs `docker compose down -v`.
set -euo pipefail
cd "$(dirname "$0")/../.."

docker compose up -d --wait postgres minio
docker compose up -d minio-init

minio_init_row() {
  docker compose ps -a --format json minio-init | python3 -c '
import json, sys
raw = sys.stdin.read().strip()
if not raw:
    raise SystemExit(1)
rows = json.loads(raw) if raw.startswith("[") else [
    json.loads(line) for line in raw.splitlines() if line.strip()
]
if not rows:
    raise SystemExit(1)
row = rows[0]
print(row.get("State", ""), row.get("ExitCode", ""))
'
}

finished=0
for _ in $(seq 1 75); do
  if parsed="$(minio_init_row)"; then
    state="${parsed%% *}"
    code="${parsed#* }"
    if [ "$state" = "exited" ]; then
      if [ "$code" != "0" ]; then
        echo "bootstrap: minio-init exited $code" >&2
        exit 1
      fi
      finished=1
      break
    fi
  fi
  sleep 2
done
if [ "$finished" -ne 1 ]; then
  echo "bootstrap: minio-init did not finish" >&2
  exit 1
fi

buckets=(marketplace-images marketplace-csv)
for bucket in "${buckets[@]}"; do
  docker compose run --rm --no-deps minio-init ls "local/$bucket" >/dev/null \
    || { echo "bootstrap: bucket $bucket missing" >&2; exit 1; }
done

make bootstrap-db

uv run --frozen uvicorn app.main:app --port 8000 --app-dir apps/api &
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
