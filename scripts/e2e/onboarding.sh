#!/usr/bin/env bash
# The ONBOARDING.md e2e (S14b T25; Done-when 7): every command the guide tells a newcomer to run
# works on a clean clone of this branch, in its own compose project on its own free ports, and the
# clone, its stack and every background process are gone afterwards (on success and on failure).
#
# Usage:
#   scripts/e2e/onboarding.sh                  clone, prepare, run every ONBOARDING command, clean up
#   scripts/e2e/onboarding.sh --prepare DIR    only allocate ports and write DIR's .worktree.env/.env
#   scripts/e2e/onboarding.sh --run DIR        only run DIR/ONBOARDING.md (DIR prepared already)
# `make e2e-onboarding` runs the first form; `make e2e` skips this script (it needs Docker and a
# clone). The commands come from scripts/ci/onboarding_commands.py, the parser the meta test uses.
set -euo pipefail
cd "$(dirname "$0")/../.."
root="$(pwd)"

# shellcheck source=scripts/e2e/lib/free_ports.sh
. scripts/e2e/lib/free_ports.sh

wait_cap="${ONBOARDING_WAIT_SECONDS:-120}"
default_ports="5432 9000 9001 8000 4010"
bg_pids=()
clone_dir=""
compose_up=0
work=""

log() { echo "onboarding: $*"; }

cleanup() {
  local pid
  for pid in "${bg_pids[@]:-}"; do
    [ -n "$pid" ] || continue
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  done
  sleep 1
  for pid in "${bg_pids[@]:-}"; do
    [ -n "$pid" ] || continue
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  done
  if [ "$compose_up" -eq 1 ] && [ -n "$clone_dir" ] && [ -d "$clone_dir" ]; then
    (cd "$clone_dir" && set -a && . ./.worktree.env && set +a && docker compose down -v --remove-orphans >/dev/null 2>&1) || true
  fi
  if [ -n "$work" ]; then rm -rf "$work"; fi
}

# prepare DIR: free ports and a fresh project name for every WORKTREE_KEYS key, written to DIR's
# .worktree.env, and DIR's .env from its .env.example with the URLs on those ports. Exit 2 when a
# port or the project would collide with this checkout's or the defaults.
prepare() {
  local dir="$1" assignments key port mine project
  assignments="$(free_ports "$dir/Makefile")" || exit 1
  if [ -n "${ONBOARDING_FORCE_PORTS:-}" ]; then  # tests only: KEY=PORT,... replaces an allocation
    local forced
    for forced in ${ONBOARDING_FORCE_PORTS//,/ }; do
      key="${forced%%=*}"
      assignments="$(printf '%s\n' "$assignments" | sed "s/^${key}=.*/${forced}/")"
    done
  fi
  mine="$(scripts/ci/worktree_env.sh exports .worktree.env)" || exit 1
  project="mkos-onb-$$-$RANDOM"
  if printf '%s\n' "$mine" | grep -qx "export COMPOSE_PROJECT_NAME=$project"; then
    echo "onboarding: project $project collides with this checkout" >&2
    exit 2
  fi
  while IFS='=' read -r key port; do
    [ -n "$key" ] || continue
    if printf '%s\n' "$mine" | grep -qx "export $key=$port"; then
      echo "onboarding: $key $port collides with this checkout" >&2
      exit 2
    fi
    case " $default_ports " in
      *" $port "*) echo "onboarding: $key $port is a default port" >&2; exit 2 ;;
    esac
  done <<<"$assignments"
  {
    echo "COMPOSE_PROJECT_NAME=$project"
    printf '%s\n' "$assignments"
  } >"$dir/.worktree.env"
  python3 - "$dir" <<'PY'
import re
import sys
from pathlib import Path

d = Path(sys.argv[1])
ports = dict(
    line.split("=", 1) for line in (d / ".worktree.env").read_text().splitlines() if "=" in line
)
pg, minio, api = ports["PG_HOST_PORT"], ports["MINIO_API_HOST_PORT"], ports["API_HOST_PORT"]
out = []
for line in (d / ".env.example").read_text(encoding="utf-8").splitlines():
    if line.startswith(("DATABASE_URL=", "ALEMBIC_DATABASE_URL=")):
        line = re.sub(r"@localhost:\d+/", f"@localhost:{pg}/", line)
    elif line.startswith("S3_ENDPOINT_URL="):
        line = re.sub(r"localhost:\d+", f"localhost:{minio}", line)
    elif line.startswith("PORT="):
        line = f"PORT={api}"
    out.append(line)
(d / ".env").write_text("\n".join(out) + "\n", encoding="utf-8")
PY
  # The clone's own files decide: a caller's URLs and ports (`make api-tests` exports DATABASE_URL)
  # must not stand in for them.
  (cd "$dir" && env -u DATABASE_URL -u ALEMBIC_DATABASE_URL -u S3_ENDPOINT_URL -u PORT \
    -u COMPOSE_PROJECT_NAME -u PG_HOST_PORT -u MINIO_API_HOST_PORT -u MINIO_CONSOLE_HOST_PORT \
    -u API_HOST_PORT -u MOCK_HOST_PORT scripts/ci/worktree_env.sh check) || exit 1
  log "prepared $dir (project $project)"
}

# run_commands DIR: every ONBOARDING command, in order; background lines are waited on.
run_commands() {
  local dir="$1" api_port listing mode wait_path reason line
  api_port="$(cd "$dir" && scripts/ci/worktree_env.sh exports .worktree.env | sed -n 's/^export API_HOST_PORT=//p')"
  listing="$(python3 "$root/scripts/ci/onboarding_commands.py" "$dir/ONBOARDING.md")" || exit 1
  while IFS=$'\x1f' read -r mode wait_path reason line; do
    [ -n "$mode" ] || continue
    local -a argv
    read -r -a argv <<<"$line"
    case "$mode" in
      skip)
        echo "SKIP $line: $reason"
        ;;
      background)
        log "background: $line"
        (cd "$dir" && exec python3 -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "${argv[@]}") &
        bg_pids+=("$!")
        local waited=0
        until curl -sf -o /dev/null "http://127.0.0.1:${api_port}${wait_path}"; do
          waited=$((waited + 1))
          if [ "$waited" -ge "$wait_cap" ]; then
            echo "onboarding: FAILED: $line never answered ${wait_path} in ${wait_cap}s" >&2
            exit 1
          fi
          sleep 1
        done
        ;;
      run)
        log "run: $line"
        (cd "$dir" && "${argv[@]}" </dev/null) || { echo "onboarding: FAILED: $line" >&2; exit 1; }
        ;;
    esac
  done <<<"$listing"
}

trap cleanup EXIT

case "${1:-}" in
  --prepare)
    prepare "${2:?usage: onboarding.sh --prepare DIR}"
    ;;
  --run)
    clone_dir="${2:?usage: onboarding.sh --run DIR}"
    run_commands "$clone_dir"
    log "OK"
    ;;
  "")
    work="$(mktemp -d)"
    clone_dir="$work/clone"
    branch="$(git rev-parse --abbrev-ref HEAD)"
    git clone --quiet --local --branch "$branch" "$root" "$clone_dir"
    prepare "$clone_dir"
    compose_up=1
    run_commands "$clone_dir"
    log "OK (every ONBOARDING command ran on a clean clone of $branch)"
    ;;
  *)
    echo "usage: onboarding.sh [--prepare DIR | --run DIR]" >&2
    exit 2
    ;;
esac
