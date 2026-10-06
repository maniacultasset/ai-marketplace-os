#!/usr/bin/env bash
# The `bootstrap` required check: CANON-ADDENDUM.md §7, reduced per ADR-0013 §4 (the image import
# omits mkos_pipeline until S14). Alembic runs after bootstrap-db (S5), then the demo seed (S9);
# the image's `migrate` and `seed` roles run once each on the compose network before the Cloud
# SQL mimic. The seed refuses any APP_ENV but local, so both runs pass APP_ENV=local.
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
uv run --frozen python -m app.cli seed --profile demo

uv run --frozen uvicorn app.main:app --port "$api_port" --app-dir apps/api &
server=$!
e2e_container="mkos-s11-e2e-${project}"
stop_server() { if kill -0 "$server" 2>/dev/null; then kill "$server"; wait "$server" || true; fi; }
cleanup() { stop_server; docker rm -f "$e2e_container" >/dev/null 2>&1 || true; }
trap cleanup EXIT

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

# S11 e2e leg: the built image serves on the compose network; one request with a known
# X-Request-ID gets its echo and a 404, and the container's log is JSON only, with exactly one
# line for that request and none for the probes. `.env` is CI's copy of .env.example
# (LOG_FORMAT=console), so the JSON format and the local env are set after it.
stop_server
docker rm -f "$e2e_container" >/dev/null 2>&1 || true
docker run -d --name "$e2e_container" --network "$network" --env-file .env \
  -e LOG_FORMAT=json -e APP_ENV=local -e APP_ROLE=api -e PORT=8080 \
  -e DATABASE_URL=postgresql+psycopg://postgres:postgres@postgres:5432/marketplace_os \
  -p "127.0.0.1:${api_port}:8080" "$image" >/dev/null
up=0
for _ in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:${api_port}/healthz" >/dev/null; then up=1; break; fi
  sleep 2
done
if [ "$up" -ne 1 ]; then
  echo "bootstrap: the API container did not answer /healthz" >&2
  docker logs "$e2e_container" >&2 || true
  exit 1
fi
curl -sf "http://127.0.0.1:${api_port}/healthz" >/dev/null
curl -sf "http://127.0.0.1:${api_port}/readyz" >/dev/null
e2e_headers="$(mktemp)"
e2e_status="$(curl -s -o /dev/null -D "$e2e_headers" -w '%{http_code}' -H 'X-Request-ID: s11-e2e' "http://127.0.0.1:${api_port}/api/v1/__s11_e2e")"
echoed="$(tr -d '\r' <"$e2e_headers" | awk -F': ' 'tolower($1) == "x-request-id" { print $2 }')"
rm -f "$e2e_headers"
if [ "$e2e_status" != "404" ] || [ "$echoed" != "s11-e2e" ]; then
  echo "bootstrap: e2e request got status '$e2e_status' and X-Request-ID '$echoed'" >&2
  exit 1
fi
sleep 1
docker logs "$e2e_container" 2>&1 | python3 -c '
import json, sys
lines = [line for line in sys.stdin.read().splitlines() if line.strip()]
if not lines:
    sys.exit("bootstrap: the API container logged nothing")
parsed = []
for line in lines:
    try:
        parsed.append(json.loads(line))
    except ValueError:
        sys.exit("bootstrap: a container log line is not JSON: %r" % line[:200])
mine = [p for p in parsed if p.get("request_id") == "s11-e2e"]
if len(mine) != 1 or mine[0].get("route") != "<unmatched>" or mine[0].get("status") != 404:
    sys.exit("bootstrap: want one access line for s11-e2e (route <unmatched>, 404), got %r" % mine)
probes = [line for line in lines if "/healthz" in line or "/readyz" in line]
if probes:
    sys.exit("bootstrap: the probes were logged: %r" % probes[:3])
print("bootstrap: s11 e2e leg OK (%d JSON lines)" % len(parsed))
'
docker rm -f "$e2e_container" >/dev/null
# The Cloud Run migrate job's path through the image (local compose password only).
docker run --rm --network "$network" -e APP_ENV=local -e APP_ROLE=migrate -e ALEMBIC_DATABASE_URL=postgresql+psycopg://postgres:postgres@postgres:5432/marketplace_os "$image"
# The image's seed role (SEED_PROFILE unset: the entrypoint's demo default); idempotent after the host seed.
docker run --rm --network "$network" -e APP_ENV=local -e APP_ROLE=seed -e DATABASE_URL=postgresql+psycopg://postgres:postgres@postgres:5432/marketplace_os -e ALEMBIC_DATABASE_URL=postgresql+psycopg://postgres:postgres@postgres:5432/marketplace_os "$image"
make bootstrap-mimic
