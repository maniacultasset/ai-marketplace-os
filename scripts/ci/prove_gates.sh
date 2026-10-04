#!/usr/bin/env bash
# Seeded-violation proofs (AC15, AC17): every required check can go red.
#
# Each leg makes a detached worktree at HEAD, applies one seeded violation, runs
# `uv sync --frozen --all-packages`, then the check's make target. A leg passes only when the
# target exits non-zero AND its log shows the expected reason. A seed that does not apply, or a
# commit that is missing, fails the leg: a setup failure never counts as the gate going red.
#
# Usage: scripts/ci/prove_gates.sh [--with-docker]
#   --with-docker adds the api-tests, tenant-isolation and bootstrap legs. They need the local
#   compose Postgres with the test databases (`make test-db`), and free ports 5432, 8000, 9000 and
#   9001 for the bootstrap legs, which pause this checkout's compose stack and restart it afterwards.
# Logs: $TMPDIR/prove-gates/<leg>.log. Never pushes; the seeded commits stay in throwaway worktrees.
set -euo pipefail
cd "$(dirname "$0")/../.."
root="$(pwd)"

with_docker=0
if [ "${1:-}" = "--with-docker" ]; then with_docker=1; fi

logs="${TMPDIR:-/tmp}/prove-gates"
mkdir -p "$logs"
work="$(mktemp -d)"
base="$(git rev-parse HEAD)"
zero_sha="0000000000000000000000000000000000000000"
summary=()
failures=0

cleanup() {
  local wt
  for wt in "$work"/*; do
    if [ -d "$wt" ]; then git worktree remove --force "$wt"; fi
  done
  git worktree prune
  rm -rf "$work"
}
trap cleanup EXIT

# ---- seed helpers (run inside the worktree) ----
replace() {  # replace <file> <old> <new>: fails when <old> is absent
  python3 - "$1" "$2" "$3" <<'PY'
import pathlib, sys
path, old, new = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = path.read_text(encoding="utf-8")
if old not in text:
    sys.exit(f"seed did not apply: {old!r} not in {path}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
PY
}
append() { printf '%s\n' "$2" >>"$1"; }
new_file() { mkdir -p "$(dirname "$1")"; printf '%s\n' "$2" >"$1"; }

commit_seeded_leak() {
  local token
  token="ghp_$(python3 -c 'import secrets, string; a = string.ascii_letters + string.digits; print("".join(secrets.choice(a) for _ in range(36)))')"
  new_file seeded_leak.txt "github_token = \"${token}\""
  git add seeded_leak.txt
  # Worktrees share .git/hooks, where `make dev` installs pre-commit; its gitleaks hook would
  # reject this commit, so hooks are disabled for the seed only. The commit is never pushed.
  git -c core.hooksPath=/dev/null -c user.name=prove-gates -c user.email=prove-gates@example.invalid \
    commit -q -m "test: seeded leak"
  [ "$(git rev-parse HEAD)" != "$base" ]
}

seed_none() { :; }
seed_unused_import() { replace apps/api/app/main.py 'from fastapi import FastAPI' $'import json\nfrom fastapi import FastAPI'; }
seed_os_environ() { append apps/api/app/main.py $'\n\ndef _seeded() -> str | None:\n    import os\n\n    return os.environ.get("X")'; }
seed_asyncpg() { append apps/api/app/main.py 'import asyncpg'; }
seed_error_code() { append apps/api/app/main.py $'\n\ndef _seeded(**kwargs: str) -> None:\n    _seeded(error_code="X")'; }
seed_expired_ignore() { append scripts/ci/pip-audit-ignore.txt 'GHSA-seed-seed-seed 2020-01-01 seeded expired entry'; }
seed_services_async_engine() { new_file apps/api/app/services/x.py $'"""Seeded."""\n\nfrom sqlalchemy.ext.asyncio import AsyncSession\n\n__all__ = ["AsyncSession"]'; }
seed_type_error() { append apps/api/app/main.py $'\n\ndef _seeded() -> int:\n    return "x"'; }
seed_failing_test() { new_file apps/api/tests/unit/test_seeded.py $'def test_seeded() -> None:\n    assert False'; }
seed_no_marker() { replace apps/api/tests/tenant_isolation/test_sentinel.py $'@pytest.mark.tenant_isolation\n' ''; }
seed_schemas_import_error() { append packages/schemas/src/mkos_schemas/__init__.py 'raise ImportError("seeded")'; }
# One hex digit in each hatchling hash. Either artifact (wheel or sdist) satisfies
# --require-hashes on its own, so both must stop matching for the pins to be broken.
seed_bad_build_hash() {
  replace build-constraints.txt '--hash=sha256:aac80bec' '--hash=sha256:bac80bec'
  replace build-constraints.txt '--hash=sha256:6b48ad40' '--hash=sha256:7b48ad40'
}
seed_drift_byte() { printf 'x' >>packages/schemas/jsonschema/.gitkeep; }
seed_drift_untracked() { new_file packages/schemas/ts/src/seeded.d.ts 'export type Seeded = 1;'; }
seed_leak() { commit_seeded_leak; }
seed_schemas_imports_app() { append packages/schemas/src/mkos_schemas/__init__.py 'import app'; }
seed_repository_imports_api() { new_file apps/api/app/repositories/x.py 'from app.api import health'; }
seed_scope_literal() { append apps/api/app/main.py '_SEEDED = "tenant_scope_checked"'; }
seed_router_sqlalchemy() { append apps/api/app/api/health.py 'from sqlalchemy import text'; }
seed_router_db() { append apps/api/app/api/v1/router.py 'import app.db.session'; }
seed_service_session() { new_file apps/api/app/services/x.py 'from app.db.session import get_engine'; }
seed_no_uv_project_env() {
  cp .env.example .env  # as the CI job does before `make bootstrap`
  replace apps/api/Dockerfile $'ENV UV_PROJECT_ENVIRONMENT=/opt/venv\n' ''
}
seed_misspelled_bucket() {
  cp .env.example .env
  replace docker-compose.yml 'local/marketplace-csv' 'local/marketplace-csx'
}

# leg <name> <gate> <expected; ;-separated extended regexes> <seed fn> <command...>
leg() {
  local name="$1" gate="$2" expected="$3" seed="$4"
  shift 4
  local wt="$work/$name" log="$logs/$name.log" rc=0 verdict="PASS" pattern
  : >"$log"
  git worktree add --quiet --detach "$wt" "$base" >>"$log" 2>&1
  if [ -d "$root/.tools" ]; then cp -R "$root/.tools" "$wt/.tools"; fi
  if ! (cd "$wt" && "$seed") >>"$log" 2>&1; then
    verdict="SETUP-FAILED (seed)"
  elif ! (cd "$wt" && uv sync --quiet --frozen --all-packages) >>"$log" 2>&1; then
    verdict="SETUP-FAILED (uv sync)"
  else
    echo "---- $* ----" >>"$log"
    set +e
    (cd "$wt" && env "$@") >>"$log" 2>&1
    rc=$?
    set -e
    if [ "$rc" -eq 0 ]; then
      verdict="FAIL (gate stayed green)"
    else
      IFS=';' read -ra patterns <<<"$expected"
      for pattern in "${patterns[@]}"; do
        if ! grep -qE -- "$pattern" "$log"; then verdict="FAIL (red, but not for: $pattern)"; fi
      done
    fi
  fi
  git worktree remove --force "$wt"
  case "$verdict" in PASS) ;; *) failures=$((failures + 1)) ;; esac
  summary+=("$(printf '%-28s %-16s exit=%-3s %s' "$name" "$gate" "$rc" "$verdict")")
}

leg api-lint-unused-import api-lint 'F401' seed_unused_import make api-lint
leg api-lint-os-environ api-lint ': R1 ' seed_os_environ make api-lint
leg api-lint-asyncpg api-lint 'TID251' seed_asyncpg make api-lint
leg api-lint-error-code api-lint ': R2 ' seed_error_code make api-lint
leg api-lint-expired-ignore api-lint 'expired' seed_expired_ignore make api-lint
leg api-lint-services-engine api-lint ': R3 ' seed_services_async_engine make api-lint
leg api-typecheck-return api-typecheck 'Incompatible return value' seed_type_error make api-typecheck
leg contracts-import-error contracts 'ImportError: seeded' seed_schemas_import_error make contracts
leg contracts-bad-hash contracts '[Hh]ash' seed_bad_build_hash make contracts
leg schemas-drift-byte schemas-drift 'diff --git' seed_drift_byte make schemas-drift
leg schemas-drift-untracked schemas-drift 'untracked generated files' seed_drift_untracked make schemas-drift
leg commit-lint-title commit-lint 'PR title does not match' seed_none \
  PR_TITLE='update stuff' HEAD_REF=feat/s1-scaffold make commit-lint
leg commit-lint-branch commit-lint 'branch name does not match' seed_none \
  PR_TITLE='feat(s1): x' HEAD_REF=feature/x make commit-lint
leg gitleaks-range gitleaks 'scanning [0-9a-f]{40}\.\.HEAD;leaks found: 1' seed_leak \
  BASE_SHA="$base" HEAD_SHA=HEAD make gitleaks
leg gitleaks-zero-base gitleaks 'zero or unknown;leaks found: 1' seed_leak \
  BASE_SHA="$zero_sha" HEAD_SHA=HEAD make gitleaks
leg lint-imports-schemas-app lint-imports 'mkos_schemas never imports app BROKEN' \
  seed_schemas_imports_app make lint-imports
leg lint-imports-layers lint-imports 'routers -> services -> repositories BROKEN' \
  seed_repository_imports_api make lint-imports
leg lint-imports-scope-grep lint-imports 'tenant_scope_checked outside' seed_scope_literal make lint-imports
leg lint-imports-router-sqla lint-imports 'app.api never imports sqlalchemy or app.db directly BROKEN' \
  seed_router_sqlalchemy make lint-imports
leg lint-imports-router-db lint-imports 'app.api never imports sqlalchemy or app.db directly BROKEN' \
  seed_router_db make lint-imports
leg lint-imports-service-db lint-imports 'app.services never imports app.db.session .*BROKEN' \
  seed_service_session make lint-imports

if [ "$with_docker" -eq 1 ]; then
  leg api-tests-failing-test api-tests 'test_seeded' seed_failing_test make api-tests
  leg tenant-isolation-no-marker tenant-isolation 'deselected' seed_no_marker make tenant-isolation
  # The bootstrap leg runs its own compose project on 5432; pause this checkout's stack meanwhile.
  docker compose stop >/dev/null 2>&1
  leg bootstrap-empty-venv bootstrap "No module named 'mkos_schemas'" seed_no_uv_project_env \
    COMPOSE_PROJECT_NAME=prove-gates-bootstrap make bootstrap
  docker compose -p prove-gates-bootstrap down -v >/dev/null 2>&1
  leg bootstrap-misspelled-bucket bootstrap 'bucket marketplace-csv missing' seed_misspelled_bucket \
    COMPOSE_PROJECT_NAME=prove-gates-bootstrap make bootstrap
  docker compose -p prove-gates-bootstrap down -v >/dev/null 2>&1
  docker compose start >/dev/null 2>&1
fi

printf '%s\n' "${summary[@]}" | tee "$logs/summary.txt"
echo "prove-gates: ${#summary[@]} legs, ${failures} not proven (logs in $logs)"
[ "$failures" -eq 0 ]
