#!/usr/bin/env bash
# The `bootstrap` required check: CANON-ADDENDUM.md §7, reduced per ADR-0013 §4 (no seed until S9,
# and the image import omits mkos_pipeline until S14). Alembic runs after bootstrap-db (S5), and the
# image's `migrate` role runs once on the compose network before the Cloud SQL mimic.
# It never copies .env and never runs `docker compose down -v`.
# The API port, the compose project and the image tag follow .worktree.env (ADR-0011 amendment).
set -euo pipefail
cd "$(dirname "$0")/../.."

worktree_exports="$(scripts/ci/worktree_env.sh exports .worktree.env)" || exit 1
eval "$worktree_exports"
scripts/ci/worktree_env.sh check
api_port="${API_HOST_PORT:-8000}"
python3 -c 'import socket, sys; s = socket.socket(); s.bind(("127.0.0.1", int(sys.argv[1]))); s.close()' "$api_port" 2>/dev/null || { echo "bootstrap: port $api_port on 127.0.0.1 is already in use" >&2; exit 1; }
compose_info="$(docker compose config --format json | python3 -c '
import json, sys
data = json.load(sys.stdin)
for name, svc in (data.get("services") or {}).items():
    for entry in svc.get("ports") or []:
        host_ip = entry.get("host_ip") if isinstance(entry, dict) else None
        if host_ip != "127.0.0.1":
            sys.exit("bootstrap: service %s publishes on %s, want 127.0.0.1" % (name, host_ip))
network = ((data.get("networks") or {}).get("default") or {}).get("name") or ""
if not network:
    sys.exit("bootstrap: compose has no default network name")
sys.stdout.write("%s %s" % (data.get("name") or "", network))
')"
read -r project network <<<"$compose_info"
image="mkos-api:ci-${project}"

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
uv run --frozen alembic -c apps/api/alembic.ini upgrade head

uv run --frozen uvicorn app.main:app --port "$api_port" --app-dir apps/api &
server=$!
stop_server() { if kill -0 "$server" 2>/dev/null; then kill "$server"; fi; }
trap stop_server EXIT

for _ in $(seq 1 30); do
  if ! kill -0 "$server" 2>/dev/null; then
    echo "bootstrap: the API server exited" >&2
    exit 1
  fi
  if curl -sf "http://127.0.0.1:${api_port}/healthz" >/dev/null; then break; fi
  sleep 2
done
curl -sf "http://127.0.0.1:${api_port}/healthz" && curl -sf "http://127.0.0.1:${api_port}/readyz"
echo

docker build -f apps/api/Dockerfile -t "$image" .
docker run --rm -e APP_ROLE=shell "$image" -c "import app, mkos_schemas"
scripts/ci/check_image.sh "$image"
# The Cloud Run migrate job's path through the image (local compose password only).
docker run --rm --network "$network" -e APP_ENV=local -e APP_ROLE=migrate -e ALEMBIC_DATABASE_URL=postgresql+psycopg://postgres:postgres@postgres:5432/marketplace_os "$image"
make bootstrap-mimic
