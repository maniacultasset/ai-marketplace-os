#!/usr/bin/env bash
# One leg of the nightly workflow (S26 T24; AC 12; PLAN D8): `.github/workflows/nightly.yml` runs
# `scripts/ci/nightly.sh <leg>` per job. Never a PR check; no secret; the stub pipeline only;
# nothing released, no cloud. Every leg runs on the runner's own Docker (compose postgres + MinIO, or a
# fresh stack), never a remote database.
set -euo pipefail
cd "$(dirname "$0")/../.."

# The legs:
#   pip-audit          the locked packages against the advisory service (ADR-0013 §6)
#   gitleaks-history   every commit reachable from HEAD
#   mutation           `make mutation` (>= 80% per core module or a waiver row)
#   fuzz               `make fuzz` with 500 examples per role
#   hypothesis-deep    the property tests with HYPOTHESIS_PROFILE=nightly (1000 examples)
#   random-order       the full api suite (both shards) with pytest-randomly seeds 1, 2, 3
#   migration-replay   upgrade -> downgrade base -> upgrade on a fresh database, each timed
#   acceptance         `make acceptance`
#   drill              `make drill-restore` and `pytest -m drill`
#
# The heavy legs (all but pip-audit and gitleaks-history) print `SKIP main unchanged in 24 h`
# and exit 0 when origin/main has no commit in 24 h and NIGHTLY_FORCE is not `true`
# (scripts/ci/nightly_skip.py).

LIGHT="pip-audit gitleaks-history"
HEAVY="mutation fuzz hypothesis-deep random-order migration-replay acceptance drill"
leg="${1:-}"
case " $LIGHT $HEAVY " in
  *" $leg "*) [ -n "$leg" ] || { echo "usage: nightly.sh <leg>" >&2; exit 2; } ;;
  *) echo "usage: nightly.sh <leg> (one of: $LIGHT $HEAVY)" >&2; exit 2 ;;
esac

if [[ " $HEAVY " == *" $leg "* ]]; then
  last="$(git log -1 --format=%ct origin/main 2>/dev/null || git log -1 --format=%ct HEAD)"
  if python3 scripts/ci/nightly_skip.py "$last" --force "${NIGHTLY_FORCE:-false}"; then
    exit 0
  fi
fi

TEST_DB_URL="postgresql+psycopg://postgres:postgres@localhost:5432/mos_test"
CI_JWT_SECRET="ci_only_not_a_real_secret_at_least_32_chars"

# the runner's compose postgres + MinIO, 0000 and the test databases (the api-tests job's setup)
with_db() {
  [ -f .env ] || cp .env.example .env
  docker compose up -d --wait postgres minio
  docker compose run --rm minio-init
  make bootstrap-db
  make test-db
}

test_env() {
  env APP_ENV=local DATABASE_URL="$TEST_DB_URL" ALEMBIC_DATABASE_URL="$TEST_DB_URL" \
    JWT_SECRET="$CI_JWT_SECRET" S3_ENDPOINT_URL=http://localhost:9000 "$@"
}

case "$leg" in
  pip-audit)
    uv run --frozen python scripts/ci/run_pip_audit.py
    ;;
  gitleaks-history)
    BASE_SHA="" HEAD_SHA="$(git rev-parse HEAD)" scripts/ci/gitleaks.sh
    ;;
  mutation)
    with_db
    make mutation
    ;;
  fuzz)
    [ -f .env ] || cp .env.example .env
    make fuzz FUZZ_MAX_EXAMPLES=500
    ;;
  hypothesis-deep)
    with_db
    test_env uv run --frozen alembic -c apps/api/alembic.ini upgrade head
    test_env uv run --frozen python -m app.cli dictionary sync
    mapfile -t files < <(grep -rlE '^(from|import) hypothesis' apps/api/tests packages/*/tests \
      --include='test_*.py' | sort)
    [ "${#files[@]}" -gt 0 ] || { echo "nightly: no property tests found" >&2; exit 1; }
    HYPOTHESIS_PROFILE=nightly test_env uv run --frozen pytest -q --no-cov \
      -m "not tenant_isolation and not cloudsql_mimic and not drill" "${files[@]}"
    ;;
  random-order)
    with_db
    for seed in 1 2 3; do
      echo "nightly: random-order seed $seed"
      PYTEST_ADDOPTS="--randomly-seed=$seed" \
        make api-tests API_TESTS_SHARD= COV_FAIL_UNDER=0
    done
    ;;
  migration-replay)
    with_db
    for step in "upgrade head" "downgrade base" "upgrade head"; do
      started="$(date +%s)"
      # shellcheck disable=SC2086 # the step is two words
      test_env uv run --frozen alembic -c apps/api/alembic.ini $step
      echo "nightly: migration-replay alembic $step took $(( $(date +%s) - started )) s"
    done
    ;;
  acceptance)
    [ -f .env ] || cp .env.example .env
    make acceptance
    ;;
  drill)
    with_db
    make drill-restore
    test_env uv run --frozen pytest -q --no-cov -m drill apps/api/tests/drill
    ;;
esac
