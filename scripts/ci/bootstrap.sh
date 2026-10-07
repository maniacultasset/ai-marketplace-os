#!/usr/bin/env bash
# The `bootstrap` required check: CANON-ADDENDUM.md §7, reduced per ADR-0013 §4 (since S14b the image
# import includes mkos_pipeline, the in-repo stub). Alembic runs after bootstrap-db (S5), then the demo seed (S9);
# the image's `migrate` and `seed` roles run once each on the compose network before the Cloud
# SQL mimic. The seed refuses any APP_ENV but local, so both runs pass APP_ENV=local.
# It never copies .env and never runs `docker compose down -v`.
# The API port, the compose project and the image tag follow .worktree.env (ADR-0011 amendment).
# The app logs in as app_rw and Alembic as migrator (ENV.md; CODE-PLAN S10 Q5): the DSNs come from
# the environment or .env (CI's copy of .env.example) with the host rewritten for the compose
# network, and the auth leg first proves the app's own login is neither superuser nor BYPASSRLS.
# The S13 worker leg boots the image as APP_ROLE=worker and stops it with SIGTERM; the S13b import
# leg runs the HTTP import flow on the image with its own worker container.
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

# The process environment wins over .env, as it does for Settings (pydantic-settings).
env_url() {
  local value="${!1:-}"
  if [ -z "$value" ] && [ -f .env ]; then value="$(grep "^$1=" .env | head -n 1 | cut -d= -f2-)"; fi
  printf '%s' "$value"
}
on_network() { sed -E 's#@(localhost|127\.0\.0\.1):[0-9]+/#@postgres:5432/#'; }
app_dsn="$(env_url DATABASE_URL | on_network)"
migrator_dsn="$(env_url ALEMBIC_DATABASE_URL | on_network)"
case "$app_dsn" in postgresql+psycopg://app_rw:*@postgres:5432/marketplace_os) ;; *) echo "bootstrap: .env DATABASE_URL must log in as app_rw" >&2; exit 1 ;; esac
case "$migrator_dsn" in postgresql+psycopg://migrator:*@postgres:5432/marketplace_os) ;; *) echo "bootstrap: .env ALEMBIC_DATABASE_URL must log in as migrator" >&2; exit 1 ;; esac

make bootstrap-db
uv run --frozen alembic -c apps/api/alembic.ini upgrade head
uv run --frozen python -m app.cli dictionary sync
uv run --frozen python -m app.cli seed --profile demo

uv run --frozen uvicorn app.main:app --port "$api_port" --app-dir apps/api &
server=$!
e2e_container="mkos-s11-e2e-${project}"
worker_container="mkos-s13-worker-${project}"
import_worker="mkos-s13b-import-worker-${project}"
stop_server() { if kill -0 "$server" 2>/dev/null; then kill "$server"; wait "$server" || true; fi; }
cleanup() {
  stop_server
  docker rm -f "$e2e_container" "$worker_container" "$import_worker" >/dev/null 2>&1 || true
}
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
docker run --rm -e APP_ROLE=shell "$image" -c "import app, mkos_schemas, mkos_pipeline"
scripts/ci/check_image.sh "$image"

# S11 e2e leg: the built image serves on the compose network; one request with a known
# X-Request-ID gets its echo and a 404, and the container's log is JSON only, with exactly one
# line for that request and none for the probes. `.env` is CI's copy of .env.example
# (LOG_FORMAT=console), so the JSON format and the local env are set after it.
stop_server
docker rm -f "$e2e_container" >/dev/null 2>&1 || true
docker run -d --name "$e2e_container" --network "$network" --env-file .env \
  -e LOG_FORMAT=json -e APP_ENV=local -e APP_ROLE=api -e PORT=8080 \
  -e DATABASE_URL="$app_dsn" -e S3_ENDPOINT_URL=http://minio:9000 \
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
# S10 auth leg (CODE-PLAN S10 C14). First, over the app's own DSN: it is app_rw, not a superuser
# and not BYPASSRLS (a superuser ignores RLS even under FORCE, DATABASE.md:129).
docker run --rm --network "$network" -e APP_ENV=local -e APP_ROLE=shell -e DATABASE_URL="$app_dsn" "$image" -c '
import os, psycopg
url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
with psycopg.connect(url, connect_timeout=5) as conn:
    row = conn.execute(
        "SELECT current_user, r.rolsuper, r.rolbypassrls FROM pg_roles r WHERE r.rolname = current_user"
    ).fetchone()
if tuple(row) != ("app_rw", False, False):
    raise SystemExit("bootstrap: the app connects as %r, want app_rw without superuser or BYPASSRLS" % (row,))
print("bootstrap: the app connects as app_rw (no superuser, no BYPASSRLS)")
'
# The one setup step that is not HTTP: no seed user has two memberships (s9 PLAN AC5), so the
# migrator gives operator@acme.test the reviewer role in bharat (idempotent).
docker compose exec -T postgres psql -U postgres -d marketplace_os -v ON_ERROR_STOP=1 -X -q -c "
SET SESSION AUTHORIZATION migrator;
INSERT INTO public.tenant_memberships (tenant_id, user_id, role)
SELECT t.id, u.id, 'reviewer' FROM public.tenants t, public.users u
 WHERE t.slug = 'bharat' AND lower(u.email) = 'operator@acme.test'
ON CONFLICT (tenant_id, user_id) DO NOTHING;"
python3 scripts/ci/auth_e2e.py "http://127.0.0.1:${api_port}"
# S12 walking-skeleton leg (CODE-PLAN S12 T15; Done-when 3): the image, as `api` and app_rw, is
# ready, then the HTTP e2e runs twice (a rerun must pass on its own fresh SKU). Each run starts
# from an empty limiter, so the auth leg's logins above never need counting.
ready="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${api_port}/readyz")"
[ "$ready" = "200" ] || { echo "bootstrap: the image's /readyz gave $ready, want 200" >&2; exit 1; }
scripts/ci/reset_rate_limits.sh "http://127.0.0.1:${api_port}"
scripts/e2e/walking_skeleton.sh "http://127.0.0.1:${api_port}"
scripts/ci/reset_rate_limits.sh "http://127.0.0.1:${api_port}"
scripts/e2e/walking_skeleton.sh "http://127.0.0.1:${api_port}"
# S13b import leg (CODE-PLAN S13 B15; AC 22): the HTTP import flow against the image. Its hooks
# start and stop the image as APP_ROLE=worker on the compose network (as app_rw, MinIO by service
# name) and run `templates load` through the image's shell role as migrator, with the flow's
# templates file mounted read-only. No other worker runs against this stack meanwhile.
export WORKER_START="docker run -d --name '$import_worker' --network '$network' -e APP_ENV=local -e APP_ROLE=worker -e LOG_FORMAT=json -e DATABASE_URL='$app_dsn' -e S3_ENDPOINT_URL=http://minio:9000 '$image' >/dev/null"
export WORKER_STOP="docker stop --time 10 '$import_worker' >/dev/null && docker rm -f '$import_worker' >/dev/null"
export TEMPLATES_LOAD="docker run --rm --network '$network' -e APP_ENV=local -e APP_ROLE=shell -e ALEMBIC_DATABASE_URL='$migrator_dsn' -v \"\$1\":/tmp/templates.json:ro '$image' -m app.cli templates load --file /tmp/templates.json"
scripts/ci/reset_rate_limits.sh "http://127.0.0.1:${api_port}"
scripts/e2e/import_flow.sh "http://127.0.0.1:${api_port}"
# S16 brand onboarding leg (CODE-PLAN S16 T22; AC10): the same worker hooks, and the staff CLI
# through the image's shell role as migrator (stdin open for the new users' passwords). It runs
# twice: a rerun must pass on its own fresh tenant.
export MKOS_CLI="docker run -i --rm --network '$network' -e APP_ENV=local -e APP_ROLE=shell -e ALEMBIC_DATABASE_URL='$migrator_dsn' '$image' -m app.cli"
scripts/ci/reset_rate_limits.sh "http://127.0.0.1:${api_port}"
scripts/e2e/brand_onboarding.sh "http://127.0.0.1:${api_port}"
scripts/ci/reset_rate_limits.sh "http://127.0.0.1:${api_port}"
scripts/e2e/brand_onboarding.sh "http://127.0.0.1:${api_port}"
docker rm -f "$e2e_container" >/dev/null
# S13 worker leg (DW11): the image as APP_ROLE=worker on the compose network, as app_rw. It must
# register its procrastinate workers within 30 s, then SIGTERM (docker stop; SIGKILL only after
# 10 s) must end it with exit 0 in under 10 s, unregistered, and no log line may carry a DSN.
docker rm -f "$worker_container" >/dev/null 2>&1 || true
docker run -d --name "$worker_container" --network "$network" -e APP_ENV=local -e APP_ROLE=worker \
  -e LOG_FORMAT=json -e DATABASE_URL="$app_dsn" "$image" >/dev/null
worker_rows() {
  docker compose exec -T postgres psql -U postgres -d marketplace_os -tAc \
    "SELECT count(*) FROM procrastinate_workers"
}
registered=0
for _ in $(seq 1 30); do
  if [ "$(worker_rows)" -ge 1 ]; then registered=1; break; fi
  sleep 1
done
if [ "$registered" -ne 1 ]; then
  echo "bootstrap: the worker registered no procrastinate_workers row within 30 s" >&2
  docker logs "$worker_container" >&2 || true
  exit 1
fi
worker_started="$(date +%s)"
docker stop --time 10 "$worker_container" >/dev/null
worker_took="$(( $(date +%s) - worker_started ))"
worker_code="$(docker inspect -f '{{.State.ExitCode}}' "$worker_container")"
if [ "$worker_code" != "0" ] || [ "$worker_took" -ge 10 ]; then
  echo "bootstrap: the worker exited $worker_code after SIGTERM in ${worker_took}s (want 0, < 10 s)" >&2
  docker logs "$worker_container" >&2 || true
  exit 1
fi
if [ "$(worker_rows)" != "0" ]; then
  echo "bootstrap: the worker left its procrastinate_workers rows behind" >&2
  exit 1
fi
if docker logs "$worker_container" 2>&1 | grep -Eq 'postgres(ql)?(\+psycopg)?://|app_rw:'; then
  echo "bootstrap: a worker log line carries a DSN" >&2
  exit 1
fi
docker rm -f "$worker_container" >/dev/null
echo "bootstrap: worker leg OK (SIGTERM exit 0 in ${worker_took}s)"
# The Cloud Run migrate job's path through the image, as migrator (local dev-only password).
docker run --rm --network "$network" -e APP_ENV=local -e APP_ROLE=migrate -e ALEMBIC_DATABASE_URL="$migrator_dsn" "$image"
# The image's seed role (SEED_PROFILE unset: the entrypoint's demo default); idempotent after the host seed.
docker run --rm --network "$network" -e APP_ENV=local -e APP_ROLE=seed -e DATABASE_URL="$app_dsn" -e ALEMBIC_DATABASE_URL="$migrator_dsn" "$image"
make bootstrap-mimic
