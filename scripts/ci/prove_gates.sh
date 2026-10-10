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
#   compose Postgres with the test databases (`make test-db`), and this checkout's host ports
#   (defaults 5432, 8000, 9000, 9001, or its `.worktree.env`). The bootstrap legs pause this
#   checkout's compose stack and restart it afterwards.
# Logs: $TMPDIR/prove-gates/<leg>.log. Never pushes; the seeded commits stay in throwaway worktrees.
set -euo pipefail
cd "$(dirname "$0")/../.."
root="$(pwd)"
worktree_exports="$(scripts/ci/worktree_env.sh exports .worktree.env)" || exit 1
eval "$worktree_exports"

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
seed_failing_test() {
  cp "$root/.env" .env
  new_file apps/api/tests/unit/test_seeded.py $'def test_seeded() -> None:\n    assert False'
}
seed_no_marker() {
  cp "$root/.env" .env
  replace apps/api/tests/tenant_isolation/conftest.py $'            item.add_marker(pytest.mark.tenant_isolation)\n' ''
}
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
seed_products_router_sqlalchemy() { append apps/api/app/api/v1/routers/products.py 'from sqlalchemy import select'; }
seed_products_router_db() { append apps/api/app/api/v1/routers/products.py 'import app.db.session'; }
seed_service_session() { new_file apps/api/app/services/x.py 'from app.db.session import get_engine'; }
seed_no_uv_project_env() {
  cp .env.example .env  # as the CI job does before `make bootstrap`
  replace apps/api/Dockerfile $'ENV UV_PROJECT_ENVIRONMENT=/opt/venv\n' ''
}
# S14b T14: the image without the pipeline stub's source cannot import mkos_pipeline.
seed_bootstrap_no_pipeline() {
  cp .env.example .env  # as the CI job does before `make bootstrap`
  replace apps/api/Dockerfile $'COPY packages/pipeline-stub/ packages/pipeline-stub/\n' ''
}
seed_misspelled_bucket() {
  cp .env.example .env
  replace docker-compose.yml 'local/marketplace-csv' 'local/marketplace-csx'
}
# S5: Alembic and squawk in api-tests (on mos_prove, so a seeded revision never stamps mos_test).
seed_alembic_drift() {
  cp "$root/.env" .env
  append apps/api/app/db/base.py $'\nfrom sqlalchemy import Column, Integer, Table  # noqa: E402\n\nTable("seeded_drift", metadata, Column("id", Integer, primary_key=True))'
}
# The seeded revision chains onto the current head: a second root would stop `upgrade head`
# with "Multiple head revisions" before the downgrade ever runs.
alembic_head() {
  python3 - <<'PY'
import pathlib, re
revisions, downs = set(), set()
for path in pathlib.Path("apps/api/alembic/versions").glob("*.py"):
    text = path.read_text(encoding="utf-8")
    rev = re.search(r'^revision\b[^=]*= *"([^"]+)"', text, re.M)
    down = re.search(r'^down_revision\b[^=]*= *"([^"]+)"', text, re.M)
    if rev:
        revisions.add(rev.group(1))
    if down:
        downs.add(down.group(1))
heads = sorted(revisions - downs)
assert len(heads) == 1, heads
print(heads[0])
PY
}
seed_roundtrip_broken() {
  local head
  head="$(alembic_head)"
  cp "$root/.env" .env
  new_file apps/api/alembic/versions/0999_seeded.py $'"""seeded"""\n\nrevision = "0999"\ndown_revision = "'"$head"$'"\nbranch_labels = None\ndepends_on = None\n\n\ndef upgrade() -> None:\n    pass\n\n\ndef downgrade() -> None:\n    raise RuntimeError("seeded downgrade failure")'
}
# S10a: 0004's guards are load-bearing. A seeded revision on the head drops lv_guard (the CHECK
# still refuses the generator, but with 23514, not MKC01); a GRANT appended to 03_append_only puts
# UPDATE on audit_log back for app_rw (the trigger answers, not the privilege).
seed_lv_guard_dropped() {
  local head
  head="$(alembic_head)"
  cp "$root/.env" .env
  new_file apps/api/alembic/versions/0998_seeded.py $'"""seeded"""\n\nfrom alembic import op\n\nrevision = "0998"\ndown_revision = "'"$head"$'"\nbranch_labels = None\ndepends_on = None\n\n\ndef upgrade() -> None:\n    op.execute("DROP TRIGGER lv_guard ON public.listing_versions")\n\n\ndef downgrade() -> None:\n    op.execute("CREATE TRIGGER lv_guard BEFORE UPDATE ON public.listing_versions FOR EACH ROW EXECUTE FUNCTION public.lv_guard()")'
}
seed_audit_update_granted() {
  cp "$root/.env" .env
  append apps/api/alembic/sql/0004/03_append_only.up.sql 'GRANT UPDATE ON public.audit_log TO app_rw;'
}
# S10b: 0005's policies are load-bearing. A seeded revision on the head drops t_tenant on
# listings (RLS stays forced, so app_rw would read nothing): the catalog assertion goes red.
seed_rls_policy_dropped() {
  local head
  head="$(alembic_head)"
  cp "$root/.env" .env
  new_file apps/api/alembic/versions/0997_seeded.py $'"""seeded"""\n\nfrom alembic import op\n\nrevision = "0997"\ndown_revision = "'"$head"$'"\nbranch_labels = None\ndepends_on = None\n\n\ndef upgrade() -> None:\n    op.execute("DROP POLICY t_tenant ON public.listings")\n\n\ndef downgrade() -> None:\n    op.execute("CREATE POLICY t_tenant ON public.listings TO app_rw USING (tenant_id = (SELECT public.app_tenant_id())) WITH CHECK (tenant_id = (SELECT public.app_tenant_id()))")'
}
# A throwaway mos_prove with 0000 (a seeded revision leg leaves its revision stamped there).
fresh_prove_db() {
  docker compose exec -T postgres dropdb -U postgres --if-exists --force mos_prove
  docker compose exec -T postgres createdb -U postgres mos_prove
  docker compose exec -T postgres psql -U postgres -v ON_ERROR_STOP=1 -q -d mos_prove -f - <scripts/db/0000_bootstrap.sql
}
seed_squawk_missing() {
  cp "$root/.env" .env
  replace scripts/ci/squawk_report.py 'DEFAULT_SQUAWK: Final = "squawk"' 'DEFAULT_SQUAWK: Final = "squawk-seeded-missing"'
}
seed_alembic_environ() { append apps/api/alembic/env.py $'\n\ndef _seeded() -> str | None:\n    import os\n\n    return os.environ.get("X")'; }
seed_cli_imported() { append apps/api/app/main.py 'import app.cli'; }
seed_image_checks_sqla() { append apps/api/app/services/image_checks.py 'import sqlalchemy'; }
seed_image_checks_repo() { append apps/api/app/services/image_checks.py 'import app.repositories'; }
seed_app_imports_mock() { append apps/api/app/main.py 'import app_mock'; }
# S3 AC9: an implemented route without its case matrix, then one still on presence-only auth.
# S10 implemented auth_me, so the S3 case-matrix legs seed a route that is still a stub.
seed_case_matrix() {
  cp "$root/.env" .env
  replace apps/api/app/api/v1/routers/tenants.py $'@stub\nasync def tenants_me(' 'async def tenants_me('
}
seed_presence_only_auth() {
  seed_case_matrix
  # S10's route() wires require()/public(); the seed adds S3's presence-only check back by hand.
  replace apps/api/app/api/v1/routers/tenants.py 'router = APIRouter()' $'from fastapi import Depends\n\nfrom app.api.v1.deps import bearer_required\n\nrouter = APIRouter(dependencies=[Depends(bearer_required)])'
  new_file apps/api/tests/unit/test_seeded_tenants_me_cases.py $'import pytest\n\n\n@pytest.mark.endpoint("tenants_me", case="success")\n@pytest.mark.endpoint("tenants_me", case="missing_auth")\n@pytest.mark.endpoint("tenants_me", case="wrong_tenant")\n@pytest.mark.endpoint("tenants_me", case="invalid_token", variant="expired")\n@pytest.mark.endpoint("tenants_me", case="invalid_token", variant="bad_signature")\n@pytest.mark.endpoint("tenants_me", case="invalid_token", variant="wrong_typ")\ndef test_seeded() -> None:\n    pass'
}
# S10c: a route registered with no policy, and the login limiter removed.
seed_route_without_role() {
  cp "$root/.env" .env
  replace apps/api/app/api/v1/surface.py '            dependencies=[_policy(op)],' '            dependencies=[] if op.op_id == "rules_list" else [_policy(op)],'
}
seed_limiter_off() {
  cp "$root/.env" .env
  replace apps/api/app/api/v1/routers/auth.py $'    await enforce_login_limit(request, body.email)\n' ''
}
# S11: the redaction processor and the request-id column default are guards.
seed_no_log_redaction() { replace apps/api/app/core/logging.py $'        _redact,\n        _finalise,' '        _finalise,'; }
seed_no_request_id_default() {
  replace apps/api/app/db/base.py 'return mapped_column(Text, default=current_request_id)' 'return mapped_column(Text)'
}
seed_bootstrap_no_set_role() {
  cp .env.example .env
  replace scripts/db/0000_bootstrap.sql $'SET ROLE migrator;\n' ''
}

seed_platform_scope_worker() { new_file apps/api/app/workers/x.py $'"""Seeded."""\n\nfrom app.db.session import platform_scope\n\n__all__ = ["platform_scope"]'; }
seed_scope_ok_service() { new_file apps/api/app/services/x.py $'"""Seeded."""\n\nfrom app.db.guard import SCOPE_OK\n\n__all__ = ["SCOPE_OK"]'; }
seed_text_in_service() { new_file apps/api/app/services/x.py $'"""Seeded."""\n\nfrom sqlalchemy import text\n\n__all__ = ["text"]'; }
seed_text_attribute_chain() { new_file apps/api/app/services/x.py $'"""Seeded."""\n\nimport sqlalchemy as sa\n\nQUERY = sa.sql.text("SELECT 1")'; }
# S13a: platform_scope in the job wrapper (R5), the job GUC outside app/db/job_context.py (R8),
# and the claim compare-and-set without its `status = 'queued'` clause.
seed_platform_scope_in_wrapper() { append apps/api/app/workers/wrapper.py 'from app.db.session import platform_scope  # noqa: F401'; }
seed_job_guc_outside_context() { append apps/api/app/repositories/jobs.py '_SEEDED_GUC = "app.job_id"'; }
seed_cas_without_status() {
  cp "$root/.env" .env
  replace apps/api/app/repositories/jobs.py $'                JobRun.status == "queued",\n                JobRun.attempt < JobRun.max_attempts,' \
    '                JobRun.attempt < JobRun.max_attempts,'
}
# S13b: the row model accepts NaN as a price again (KNOWN-ISSUES.md:47), and a module outside the
# storage adapter imports boto3.
seed_row_model_nan() {
  cp "$root/.env" .env
  replace apps/api/app/services/imports/row_model.py 'MONEY_PATTERN: Final = re.compile(r"[0-9]{1,10}(\.[0-9]{1,2})?")' \
    'MONEY_PATTERN: Final = re.compile(r"[0-9]{1,10}(\.[0-9]{1,2})?|nan|NaN")'
}
seed_boto3_outside_adapter() { append apps/api/app/services/jobs.py 'import boto3  # noqa: F401'; }
seed_engine_in_worker() { new_file apps/api/app/workers/x.py $'"""Seeded."""\n\nfrom app.db.session import get_engine\n\n__all__ = ["get_engine"]'; }
seed_iso_no_filter() {
  cp "$root/.env" .env
  replace apps/api/app/repositories/base.py 'select(self.model).where(criterion).execution_options' 'select(self.model).execution_options'
}
seed_iso_no_guard() {
  cp "$root/.env" .env
  replace apps/api/app/db/session.py $'event.listen(AppSession, "do_orm_execute", check_scope)\n' ''
}
seed_iso_all_mappers() {
  cp "$root/.env" .env
  replace apps/api/app/db/guard.py 'hits = referenced_tables(state.statement) & tenant_tables()' 'hits = {m.local_table.name for m in state.all_mappers} & tenant_tables()'
}
seed_iso_table_only() {
  cp "$root/.env" .env
  replace apps/api/app/db/guard.py $'if isinstance(element, TableClause):\n' $'if isinstance(element, Table):\n'
  replace apps/api/app/db/guard.py 'if isinstance(table, TableClause):' 'if isinstance(table, Table):'
}
seed_iso_no_before_flush() {
  cp "$root/.env" .env
  replace apps/api/app/db/session.py $'event.listen(AppSession, "before_flush", check_flush)\n' ''
}
seed_iso_env() { cp "$root/.env" .env; }

# S15: the decrypt path from a router (import contract), the hash branch without its
# `token_enc IS NULL` (the public callback could read and delete a pending row), and a revoke that
# keeps the ciphertext.
seed_unseal_from_router() { append apps/api/app/api/v1/routers/marketplaces.py 'import app.adapters.credentials  # noqa: F401'; }
seed_hash_branch_pending() {
  cp "$root/.env" .env
  replace apps/api/alembic/versions/0006_oauth_pending.py \
    'f"USING (state_hash = {HASH_GUC} AND token_enc IS NULL)",
    ),
    (
        "oauth_states_hash_delete",' \
    'f"USING (state_hash = {HASH_GUC})",
    ),
    (
        "oauth_states_hash_delete",'
  replace apps/api/alembic/versions/0006_oauth_pending.py \
    'f"USING (state_hash = {HASH_GUC} AND token_enc IS NULL)",' 'f"USING (state_hash = {HASH_GUC})",'
}
seed_revoke_no_shred() {
  cp "$root/.env" .env
  replace apps/api/app/repositories/marketplace_accounts.py \
    "SET status = 'revoked', token_enc = NULL, kms_key_version = NULL, " \
    "SET status = 'revoked', kms_key_version = NULL, "
}
# Re-migrate mos_iso with this checkout's revisions (after a leg migrated it with seeded ones).
remigrate_iso() {
  local url="postgresql+psycopg://postgres:postgres@localhost:${PG_HOST_PORT:-5432}/mos_iso"
  APP_ENV=local APP_ROLE=migrate ALEMBIC_DATABASE_URL="$url" uv run --frozen alembic -c apps/api/alembic.ini downgrade 0005_rls
  APP_ENV=local APP_ROLE=migrate ALEMBIC_DATABASE_URL="$url" uv run --frozen alembic -c apps/api/alembic.ini upgrade head
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
leg api-lint-alembic-environ api-lint ': R1 ' seed_alembic_environ make api-lint
leg api-typecheck-return api-typecheck 'Incompatible return value' seed_type_error make api-typecheck
leg contracts-import-error contracts 'ImportError: seeded' seed_schemas_import_error make contracts
leg contracts-bad-hash contracts '[Hh]ash' seed_bad_build_hash make contracts
leg schemas-drift-byte schemas-drift 'diff --git' seed_drift_byte make schemas-drift
seed_contracts_ebay() { replace packages/schemas/fixtures/CanonicalListing.json '"marketplace": "amazon"' '"marketplace": "ebay"'; }
seed_pass_underscore() {  # WireModel's flag (ImageReport is a WireModel), not StrictModel's
  replace packages/schemas/src/mkos_schemas/base.py \
    $'extra="ignore",\n        validate_by_name=True,\n        validate_by_alias=True,\n        serialize_by_alias=True,' \
    $'extra="ignore",\n        validate_by_name=True,\n        validate_by_alias=True,\n        serialize_by_alias=False,'
}
seed_missing_import_fixture() { rm -f packages/schemas/fixtures/ImportReport.json; }
seed_no_fixtures() { rm -rf packages/schemas/fixtures; }
seed_drift_hand_edit() {
  printf '\n' >>packages/schemas/jsonschema/CanonicalListing.schema.json
  git add packages/schemas/jsonschema/CanonicalListing.schema.json
  git -c core.hooksPath=/dev/null -c user.name=prove-gates -c user.email=prove-gates@example.invalid \
    commit -q -m "test: seeded schema edit"
}
seed_commit() {  # seed_commit <path>: commit one seeded edit to a generated file
  git add -- "$1"
  git -c core.hooksPath=/dev/null -c user.name=prove-gates -c user.email=prove-gates@example.invalid \
    commit -q -m "test: seeded generated-file edit"
}
# S3: a committed hand edit to the generated TS types or to openapi.json.
seed_drift_api_dts() {
  printf '\nexport type Seeded = 1;\n' >>packages/schemas/ts/src/api.d.ts
  seed_commit packages/schemas/ts/src/api.d.ts
}
seed_drift_openapi() {
  replace packages/schemas/jsonschema/openapi.json '"title": "AI Marketplace OS API"' '"title": "Seeded API"'
  seed_commit packages/schemas/jsonschema/openapi.json
}
# S3: a response field removed from the contract (the leg re-exports openapi.json, then oasdiff
# compares it with this checkout's committed copy).
seed_response_field_removed() {
  replace apps/api/app/api/v1/schemas/products.py $'class ProductOut(WireModel):\n    id: UUID\n    sku: Sku\n' $'class ProductOut(WireModel):\n    id: UUID\n'
}
# S3 fix round 1 (R1-1): oasdiff rates these INFO; scripts/ci/oasdiff-levels.txt makes them red.
seed_optional_field_removed() {
  replace apps/api/app/api/pagination.py \
    $'    next_cursor: Annotated[str | None, Field(max_length=MAX_CURSOR_LENGTH)] = None\n' ''
}
seed_error_status_removed() {
  replace apps/api/app/api/v1/surface.py \
    $'    for status, codes in op.error_codes.items():\n        out[status] = {' \
    $'    for status, codes in op.error_codes.items():\n        if status == 404:\n            continue\n        out[status] = {'
}
seed_drift_model_change() { replace packages/schemas/src/mkos_schemas/listing.py 'max_length=500' 'max_length=501'; }
seed_schemas_sqlalchemy() { append packages/schemas/src/mkos_schemas/__init__.py 'import sqlalchemy'; }
seed_schemas_type_error() { append packages/schemas/src/mkos_schemas/enums.py $'\n\ndef _seeded() -> int:\n    return "x"'; }
seed_schema_failing_test() { new_file packages/schemas/tests/test_seeded.py $'def test_seeded() -> None:\n    assert False'; }

leg schemas-drift-untracked schemas-drift 'untracked generated files' seed_drift_untracked make schemas-drift
leg contracts-broken-fixture contracts 'CanonicalListing' seed_contracts_ebay make contracts
leg contracts-pass-underscore contracts 'pass_ emitted' seed_pass_underscore make contracts
leg contracts-missing-fixture contracts 'missing golden fixture: ImportReport' seed_missing_import_fixture make contracts
leg contracts-no-fixtures contracts 'no golden fixtures' seed_no_fixtures make contracts
leg schemas-drift-hand-edit schemas-drift 'diff --git' seed_drift_hand_edit make schemas-drift
leg schemas-drift-model-change schemas-drift 'diff --git' seed_drift_model_change make schemas-drift
leg schemas-drift-api-dts schemas-drift 'diff --git a/packages/schemas/ts/src/api.d.ts' seed_drift_api_dts make schemas-drift
leg schemas-drift-openapi schemas-drift 'diff --git a/packages/schemas/jsonschema/openapi.json' seed_drift_openapi make schemas-drift
leg lint-imports-schemas-sqla lint-imports 'mkos_schemas imports no app, web or DB framework BROKEN' \
  seed_schemas_sqlalchemy make lint-imports
leg api-typecheck-schemas api-typecheck 'Incompatible return value' seed_schemas_type_error make api-typecheck
leg commit-lint-title commit-lint 'PR title does not match' seed_none \
  PR_TITLE='update stuff' HEAD_REF=feat/s1-scaffold make commit-lint
leg commit-lint-branch commit-lint 'branch name does not match' seed_none \
  PR_TITLE='feat(s1): x' HEAD_REF=feature/x make commit-lint
leg gitleaks-range gitleaks 'scanning [0-9a-f]{40}\.\.HEAD;leaks found: 1' seed_leak \
  BASE_SHA="$base" HEAD_SHA=HEAD make gitleaks
leg gitleaks-zero-base gitleaks 'zero or unknown;leaks found: 1' seed_leak \
  BASE_SHA="$zero_sha" HEAD_SHA=HEAD make gitleaks
leg lint-imports-schemas-app lint-imports 'mkos_schemas imports no app, web or DB framework BROKEN' \
  seed_schemas_imports_app make lint-imports
leg lint-imports-layers lint-imports 'routers -> services -> repositories BROKEN' \
  seed_repository_imports_api make lint-imports
leg lint-imports-scope-grep lint-imports 'tenant_scope_checked outside' seed_scope_literal make lint-imports
leg lint-imports-router-sqla lint-imports 'app.api never imports sqlalchemy or app.db directly BROKEN' \
  seed_router_sqlalchemy make lint-imports
leg lint-imports-router-db lint-imports 'app.api never imports sqlalchemy or app.db directly BROKEN' \
  seed_router_db make lint-imports
# S12 T13 (Done-when 2): the real products router, not only the health and v1 routers.
leg lint-imports-products-sqla lint-imports 'app.api never imports sqlalchemy or app.db directly BROKEN' \
  seed_products_router_sqlalchemy make lint-imports
leg lint-imports-products-db lint-imports 'app.api never imports sqlalchemy or app.db directly BROKEN' \
  seed_products_router_db make lint-imports
leg lint-imports-service-db lint-imports 'app.services never imports app.db.session .*BROKEN' \
  seed_service_session make lint-imports
leg lint-imports-cli-imported lint-imports 'nothing imports app.cli BROKEN' seed_cli_imported make lint-imports
leg lint-imports-image-checks-sqla lint-imports 'image checks are pure BROKEN' seed_image_checks_sqla make lint-imports
leg lint-imports-image-checks-repo lint-imports 'image checks are pure BROKEN' seed_image_checks_repo make lint-imports
leg lint-imports-app-imports-mock lint-imports 'app never imports app_mock BROKEN' seed_app_imports_mock make lint-imports
# S3 T22: a contract edit at an already tagged version. The tag is made only inside a throwaway
# clone (a worktree shares this repo's tags), so no schemas-v* tag ever lands here. The clone tags
# the tree's current contract version and forces it, because the clone inherits this repo's real
# schemas-v* tags (schemas-v1.0.0 exists since S16; the tree is at 1.1.0).
leg contracts-schemas-tagged-unbumped contracts 'already tagged; bump per CHANGELOG rule' seed_none \
  sh -c 'd="$(mktemp -d)"; trap "rm -rf \"\$d\"" EXIT; git clone -q --no-checkout "$PWD" "$d/r" && cd "$d/r" && git checkout -q "$(git -C "$OLDPWD" rev-parse HEAD)" && v="$(python3 -c "import json; print(json.load(open(\"packages/schemas/ts/package.json\"))[\"version\"])")" && git tag -f "schemas-v$v" >/dev/null && printf "\n" >>packages/schemas/jsonschema/openapi.json && git add packages/schemas/jsonschema/openapi.json && git -c core.hooksPath=/dev/null -c user.name=prove-gates -c user.email=prove-gates@example.invalid commit -q -m "test: seeded post-tag edit" && make contracts'

leg lint-imports-platform-scope-worker lint-imports ': R5 ' seed_platform_scope_worker make lint-imports
leg lint-imports-platform-scope-in-wrapper lint-imports ': R5 ' seed_platform_scope_in_wrapper make lint-imports
leg lint-imports-job-guc-outside-context lint-imports ': R8 ' seed_job_guc_outside_context make lint-imports
leg lint-imports-scope-ok-service lint-imports ': R6 ' seed_scope_ok_service make lint-imports
leg api-lint-text-in-service api-lint ': R4 ' seed_text_in_service make api-lint
leg api-lint-text-attribute-chain api-lint ': R4 ' seed_text_attribute_chain make api-lint
leg api-lint-engine-in-worker api-lint ': R4 ' seed_engine_in_worker make api-lint
leg lint-imports-boto3-outside-adapter lint-imports 'boto3 and botocore only in app.adapters.storage BROKEN' \
  seed_boto3_outside_adapter make lint-imports

# S14a: the Rules Engine stays pure (AC 11) and its branch coverage is gated (AC 12).
seed_engine_openai() { new_file apps/api/app/services/rules_engine/x.py $'"""Seeded."""\n\nimport openai\n\nCLIENT = openai'; }
seed_engine_sqla() { append apps/api/app/services/rules_engine/__init__.py 'import sqlalchemy'; }
seed_engine_repositories() { append apps/api/app/services/rules_engine/__init__.py 'import app.repositories'; }
seed_engine_impure() { append apps/api/app/services/rules_engine/text.py 'import time'; }
seed_engine_coverage() {  # an untested function with enough branches to drop below 90%
  python3 - <<'PY'
import pathlib
body = "".join(f"    if x == {i}:\n        return {i}\n" for i in range(40))
path = pathlib.Path("apps/api/app/services/rules_engine/report.py")
path.write_text(path.read_text(encoding="utf-8") + f"\n\ndef _seeded(x: int) -> int:\n{body}    return -1\n", encoding="utf-8")
PY
}
leg api-lint-engine-openai api-lint 'TID251' seed_engine_openai make api-lint
# S16 T21: openai stays in embeddings.py; the chunker stays pure
seed_openai_in_chunker() { append apps/api/app/services/brand_chunking.py $'\nimport openai  # noqa: F401'; }
seed_chunker_imports_db() { append apps/api/app/services/brand_chunking.py $'\nimport app.db.session  # noqa: F401'; }
leg lint-openai-outside-embeddings api-lint 'TID251' seed_openai_in_chunker make api-lint
leg lint-imports-chunker-db lint-imports 'brand chunking is pure BROKEN' seed_chunker_imports_db make lint-imports
leg lint-imports-engine-sqla lint-imports 'app.services.rules_engine is pure .*BROKEN' seed_engine_sqla make lint-imports
leg lint-imports-engine-repositories lint-imports 'app.services.rules_engine is pure .*BROKEN' \
  seed_engine_repositories make lint-imports
leg lint-imports-unseal-from-router lint-imports 'only app.adapters and app.workers import the decrypt path BROKEN' \
  seed_unseal_from_router make lint-imports

# S14b T16: the production admin kill switch (A§14) is the only guard the killswitch test trips.
seed_admin_prod_guard_removed() {
  cp "$root/.env" .env
  replace apps/api/app/core/config.py $'    if s.app_env == "production" and s.admin_enabled:\n        errors.append("APP_ENV=production refuses ADMIN_ENABLED=true")\n' ''
}
# S14b T2: the pipeline stub stays pure (AC 4) and only the workers may import it.
seed_pipeline_imports_app() { append packages/pipeline-stub/src/mkos_pipeline/graph.py 'import app'; }
seed_service_imports_pipeline() { new_file apps/api/app/services/x.py $'"""Seeded."""\n\nimport mkos_pipeline\n\nSEEDED = mkos_pipeline'; }
seed_pipeline_environ() { append packages/pipeline-stub/src/mkos_pipeline/graph.py $'\n\ndef _seeded() -> str | None:\n    import os\n\n    return os.environ.get("X")'; }
leg lint-imports-pipeline-app lint-imports 'mkos_pipeline imports no app, web, DB or queue framework BROKEN' \
  seed_pipeline_imports_app make lint-imports
leg lint-imports-service-imports-pipeline lint-imports 'only app.workers imports mkos_pipeline BROKEN' \
  seed_service_imports_pipeline make lint-imports
leg api-lint-pipeline-environ api-lint ': R1 ' seed_pipeline_environ make api-lint
# S14b T17: only app.main may import the staff admin.
seed_admin_imported() { append apps/api/app/api/health.py 'import app.admin'; }
leg lint-imports-admin-imported lint-imports 'only app.main imports app.admin BROKEN' seed_admin_imported make lint-imports
# S20 T20 (AC5): a service reaching the mutating adapter, I/O in the pure preview mapping, and an
# implemented preview route that drops one of its marked cases.
seed_adapter_from_service() { append apps/api/app/services/listing_preview.py 'import app.adapters.amazon.adapter  # noqa: F401'; }
seed_mapping_httpx() { append apps/api/app/adapters/amazon/mapping.py 'import httpx  # noqa: F401'; }
seed_preview_case_matrix() {
  cp "$root/.env" .env
  replace apps/api/tests/integration/test_listings_preview.py $'@pytest.mark.endpoint("listings_preview", case="conflict")\n' ''
}
# S19 T16: the API refuses a maker before any SQL (the database's MKC01 is only the backstop),
# a stale If-Match is refused, and S17b's image gate stays wired into the approve.
seed_no_api_maker_checker() {
  cp "$root/.env" .env
  replace apps/api/app/services/review.py \
    'if approver_refusal(approver, await _chain(ctx, listing.id, version.id)) is not None:' \
    'if False:'
}
seed_no_if_match() {
  cp "$root/.env" .env
  replace apps/api/app/services/listing_edit.py \
    '    if if_match != listing.current_version_id:' '    if False:'
}
seed_no_image_gate() {
  cp "$root/.env" .env
  replace apps/api/app/services/review.py '    _image_gate(ctx, listing, version)' '    pass'
}
# S19 T24: an error is refused before the dismissal's UPDATE, and the issue inbox keeps its tenant
# filter (an unscoped select is the guard's CrossTenantQueryError).
seed_dismiss_error_precheck() {
  cp "$root/.env" .env
  replace apps/api/app/services/issues.py \
    'if issue.status != "open" or issue.severity != "warning":' 'if issue.status != "open":'
}
seed_issues_unscoped() {
  cp "$root/.env" .env
  replace apps/api/app/repositories/listing_issues.py \
    'stmt = self._select().where(ListingIssue.status == filters.status)' \
    'stmt = __import__("sqlalchemy").select(ListingIssue).where(ListingIssue.status == filters.status)'
}
leg lint-imports-adapter-from-service lint-imports 'only app.adapters and app.workers reach the marketplace adapter and client BROKEN' \
  seed_adapter_from_service make lint-imports
leg lint-imports-mapping-httpx lint-imports 'adapter mapping, endpoint check, error map and registry do no I/O BROKEN' \
  seed_mapping_httpx make lint-imports
# S21 T19, T24 (AC11, AC13): the status sync reaching a mutating adapter directly, and an
# implemented publish route that drops one of its marked cases.
seed_sync_imports_adapter() { append apps/api/app/workers/status_sync.py 'import app.workers.adapters  # noqa: F401'; }
seed_publish_case_matrix() {
  cp "$root/.env" .env
  replace apps/api/tests/integration/test_listings_publish_api.py $'@pytest.mark.endpoint("listings_publish", case="conflict")\n' ''
}
leg lint-imports-sync-imports-adapter lint-imports 'the status sync reaches the marketplace only through the reader view BROKEN' \
  seed_sync_imports_adapter make lint-imports
# S22 T17 (AC11): the same two contracts hold for Flipkart: no service reaches the Flipkart adapter,
# and the Flipkart mapping stays pure.
seed_flipkart_adapter_from_service() { append apps/api/app/services/listing_preview.py 'import app.adapters.flipkart.adapter  # noqa: F401'; }
seed_flipkart_mapping_httpx() { append apps/api/app/adapters/flipkart/mapping.py 'import httpx  # noqa: F401'; }
leg lint-imports-flipkart-adapter-from-service lint-imports 'only app.adapters and app.workers reach the marketplace adapter and client BROKEN' \
  seed_flipkart_adapter_from_service make lint-imports
leg lint-imports-flipkart-mapping-httpx lint-imports 'adapter mapping, endpoint check, error map and registry do no I/O BROKEN' \
  seed_flipkart_mapping_httpx make lint-imports

# S17b T17: the image check child stays pure, the image repository keeps its tenant filter, and
# the job refuses an image key outside its tenant's prefix before any read.
seed_image_child_config() { append apps/api/app/services/image_check_child.py 'import app.core.config'; }
seed_images_no_tenant_filter() {
  cp "$root/.env" .env
  replace apps/api/app/repositories/images.py $'        stmt = self._select().where(\n            Image.id == _uuid("image_id", image_id), Image.deleted_at.is_(None)' $'        stmt = select(Image).where(\n            Image.id == _uuid("image_id", image_id), Image.deleted_at.is_(None)'
}
seed_image_key_prefix() {
  replace apps/api/app/services/image_compliance.py $'    if parsed is None or parsed.tenant_id != ctx.tenant_id:\n        raise _failed("image key outside the tenant prefix", ctx)\n' ''
}
leg lint-imports-image-child-config lint-imports 'the image check child imports no app layer, DB, storage or HTTP client BROKEN' seed_image_child_config make lint-imports

# S18 T20: generation never publishes, the validate_rules port call cap holds, the listings
# repository keeps its tenant filter, and app/workers/budget.py is the R5 allowlist entry it needs.
seed_generation_publishes() { append apps/api/app/services/generation_persist.py $'\nfrom mkos_schemas.enums import JobKind as _SeededKind\n\nSEEDED_PUBLISH = _SeededKind.LISTING_PUBLISH'; }
seed_validate_rules_uncapped() { replace apps/api/app/services/pipeline_deps.py $'        if self._validate_calls > MAX_RULES_CALLS:\n' $'        if False:\n'; }
seed_listings_no_tenant_filter() {
  cp "$root/.env" .env
  replace apps/api/app/repositories/listings.py $'        stmt = (\n            self._select()\n            .where(Listing.id == listing_id)' $'        stmt = (\n            __import__("sqlalchemy").select(Listing)\n            .where(Listing.id == listing_id)'
}
seed_budget_not_allowlisted() { replace scripts/ci/check_repo_rules.py $'    "apps/api/app/workers/budget.py",\n' ''; }
leg lint-imports-budget-not-allowlisted lint-imports ': R5 ' seed_budget_not_allowlisted make lint-imports

if [ "$with_docker" -eq 1 ]; then
  self_project="$(docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')"
  leg api-tests-failing-test api-tests 'test_seeded' seed_failing_test COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg api-tests-schema-test api-tests 'test_seeded' seed_schema_failing_test COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg api-tests-case-matrix api-tests 'tenants_me: missing endpoint cases' seed_case_matrix COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg api-tests-no-log-redaction api-tests 'test_redaction_logs' seed_no_log_redaction COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg api-tests-no-request-id-default api-tests 'test_request_id_stamped_on_rows|test_every_request_id_column_stamped' seed_no_request_id_default COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg api-tests-presence-only-auth api-tests 'tenants_me: presence-only bearer_required' seed_presence_only_auth COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg api-tests-route-without-role api-tests 'test_every_route_declares_role' seed_route_without_role COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg api-tests-limiter-off api-tests 'test_rate_limit_sixth_login_429' seed_limiter_off COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg api-tests-cas-without-status api-tests 'test_claim_cas_drops_duplicate' seed_cas_without_status COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg api-tests-row-model-nan api-tests 'test_row_model_nan_infinity_row_error' seed_row_model_nan COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg contracts-response-field-removed contracts 'response-required-property-removed' seed_response_field_removed CONTRACTS_BASE_REF="$base" sh -c 'make -C packages/schemas openapi && make contracts'
  leg contracts-response-optional-property-removed contracts 'response-optional-property-removed' seed_optional_field_removed CONTRACTS_BASE_REF="$base" sh -c 'make -C packages/schemas openapi && make contracts'
  leg contracts-response-non-success-status-removed contracts 'response-non-success-status-removed' seed_error_status_removed CONTRACTS_BASE_REF="$base" sh -c 'make -C packages/schemas openapi && make contracts'
  leg api-tests-admin-prod-guard-removed api-tests 'test_admin_killswitch_enabled_exits' seed_admin_prod_guard_removed COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg api-tests-engine-impure api-tests 'test_rules_engine_import_allowlist' seed_engine_impure COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg api-tests-engine-coverage api-tests 'rules_engine branch coverage' seed_engine_coverage COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg api-tests-revoke-no-shred api-tests 'ck_ma_revoked_shredded|test_revoke_crypto_shreds' seed_revoke_no_shred COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg api-tests-preview-case-matrix api-tests 'listings_preview: missing endpoint cases' seed_preview_case_matrix COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg api-tests-no-api-maker-checker api-tests 'test_api_maker_checker_refuses_before_sql' seed_no_api_maker_checker COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg api-tests-no-if-match api-tests 'test_patch_stale_409' seed_no_if_match COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg api-tests-approve-image-gate api-tests 'test_approve_blocked_unchecked_image' seed_no_image_gate COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg api-tests-dismiss-error-precheck api-tests 'test_issue_dismiss_error_409' seed_dismiss_error_precheck COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg api-tests-publish-case-matrix api-tests 'listings_publish: missing endpoint cases' seed_publish_case_matrix COMPOSE_PROJECT_NAME="$self_project" make api-tests
  # A throwaway database with 0000, so the seeded revision legs never stamp mos_test. The broken
  # downgrade leaves its revision stamped there, so that leg runs last.
  docker compose exec -T postgres dropdb -U postgres --if-exists --force mos_prove
  docker compose exec -T postgres createdb -U postgres mos_prove
  docker compose exec -T postgres psql -U postgres -v ON_ERROR_STOP=1 -q -d mos_prove -f - <scripts/db/0000_bootstrap.sql
  leg api-tests-alembic-drift api-tests 'New upgrade operations detected' seed_alembic_drift COMPOSE_PROJECT_NAME="$self_project" TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:${PG_HOST_PORT:-5432}/mos_prove make api-tests
  leg api-tests-squawk-missing api-tests 'squawk did not check the SQL' seed_squawk_missing COMPOSE_PROJECT_NAME="$self_project" TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:${PG_HOST_PORT:-5432}/mos_prove make api-tests
  leg api-tests-roundtrip-broken api-tests 'seeded downgrade failure' seed_roundtrip_broken COMPOSE_PROJECT_NAME="$self_project" TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:${PG_HOST_PORT:-5432}/mos_prove make api-tests
  docker compose exec -T postgres dropdb -U postgres --force mos_prove
  fresh_prove_db
  leg api-tests-lv-guard-dropped api-tests 'test_db_maker_checker_generator' seed_lv_guard_dropped COMPOSE_PROJECT_NAME="$self_project" TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:${PG_HOST_PORT:-5432}/mos_prove make api-tests
  fresh_prove_db
  leg api-tests-audit-update-granted api-tests 'test_audit_append_only' seed_audit_update_granted COMPOSE_PROJECT_NAME="$self_project" TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:${PG_HOST_PORT:-5432}/mos_prove make api-tests
  fresh_prove_db
  leg tenant-isolation-policy-dropped api-tests 'test_rls_forced_everywhere' seed_rls_policy_dropped COMPOSE_PROJECT_NAME="$self_project" TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:${PG_HOST_PORT:-5432}/mos_prove make api-tests
  docker compose exec -T postgres dropdb -U postgres --force mos_prove
  leg tenant-isolation-no-marker tenant-isolation 'deselected' seed_no_marker COMPOSE_PROJECT_NAME="$self_project" make tenant-isolation
  leg tenant-isolation-no-filter tenant-isolation 'test_list_returns_only_own_rows' seed_iso_no_filter COMPOSE_PROJECT_NAME="$self_project" make tenant-isolation
  leg tenant-isolation-brand-ingest-no-tenant-filter tenant-isolation 'test_brand_chunk_search_ingested_isolation' seed_iso_no_filter COMPOSE_PROJECT_NAME="$self_project" make tenant-isolation
  leg tenant-isolation-no-guard tenant-isolation 'test_raw_select_raises_guard' seed_iso_no_guard COMPOSE_PROJECT_NAME="$self_project" make tenant-isolation
  leg tenant-isolation-superuser tenant-isolation 'test_isolation_runs_as_app_rw' seed_iso_env COMPOSE_PROJECT_NAME="$self_project" make tenant-isolation ISO_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:${PG_HOST_PORT:-5432}/mos_iso
  leg tenant-isolation-guard-all-mappers tenant-isolation 'test_raw_select_raises_guard\[[a-z_]+-(join|subquery|exists|cte|select_from|core_table)\]' seed_iso_all_mappers COMPOSE_PROJECT_NAME="$self_project" make tenant-isolation
  leg tenant-isolation-guard-table-only tenant-isolation 'test_raw_select_raises_guard\[[a-z_]+-table_clause\]' seed_iso_table_only COMPOSE_PROJECT_NAME="$self_project" make tenant-isolation
  leg tenant-isolation-no-before-flush tenant-isolation 'test_flush_rejects_other_tenant' seed_iso_no_before_flush COMPOSE_PROJECT_NAME="$self_project" make tenant-isolation
  leg tenant-isolation-images-no-tenant-filter tenant-isolation 'test_iso_images_repository_reads' seed_images_no_tenant_filter COMPOSE_PROJECT_NAME="$self_project" make tenant-isolation
  leg tenant-isolation-issues-unscoped tenant-isolation 'test_iso_issue_and_task_reads_other_tenant_empty' seed_issues_unscoped COMPOSE_PROJECT_NAME="$self_project" make tenant-isolation
  leg api-tests-image-key-prefix api-tests 'test_runner_rejects_foreign_key_prefix' seed_image_key_prefix COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg tenant-isolation-listings-no-tenant-filter tenant-isolation 'test_iso_generation_repositories_other_tenant_empty' seed_listings_no_tenant_filter COMPOSE_PROJECT_NAME="$self_project" make tenant-isolation
  leg api-tests-generation-publishes api-tests 'test_generation_never_publishes' seed_generation_publishes COMPOSE_PROJECT_NAME="$self_project" make api-tests
  leg api-tests-validate-rules-cap api-tests 'test_validate_rules_port_call_cap' seed_validate_rules_uncapped COMPOSE_PROJECT_NAME="$self_project" make api-tests
  # mos_iso is migrated already: the leg downgrades it below 0007 so the seeded revision applies,
  # and remigrate_iso puts this checkout's revisions back afterwards.
  leg tenant-isolation-hash-branch-pending tenant-isolation 'hides_pending_row' seed_hash_branch_pending COMPOSE_PROJECT_NAME="$self_project" sh -c 'APP_ENV=local APP_ROLE=migrate ALEMBIC_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:${PG_HOST_PORT:-5432}/mos_iso uv run --frozen alembic -c apps/api/alembic.ini downgrade 0005_rls && make tenant-isolation'
  remigrate_iso >>"$logs/tenant-isolation-hash-branch-pending.log" 2>&1
  # Bootstrap legs use their own project on this checkout's host ports; pause this stack meanwhile.
  docker compose stop >/dev/null 2>&1
  leg bootstrap-empty-venv bootstrap "No module named 'mkos_schemas'" seed_no_uv_project_env COMPOSE_PROJECT_NAME="prove-gates-bootstrap-${self_project}" DATABASE_URL=postgresql+psycopg://app_rw:ci_only_app_rw_not_a_real_password@localhost:${PG_HOST_PORT:-5432}/marketplace_os ALEMBIC_DATABASE_URL=postgresql+psycopg://migrator:ci_only_migrator_not_a_real_password@localhost:${PG_HOST_PORT:-5432}/marketplace_os S3_ENDPOINT_URL=http://localhost:${MINIO_API_HOST_PORT:-9000} PORT=${API_HOST_PORT:-8000} make bootstrap
  docker compose -p "prove-gates-bootstrap-${self_project}" down -v >/dev/null 2>&1
  docker image rm -f "mkos-api:ci-prove-gates-bootstrap-${self_project}" >/dev/null 2>&1
  leg bootstrap-no-pipeline bootstrap "No module named 'mkos_pipeline'" seed_bootstrap_no_pipeline COMPOSE_PROJECT_NAME="prove-gates-bootstrap-${self_project}" DATABASE_URL=postgresql+psycopg://app_rw:ci_only_app_rw_not_a_real_password@localhost:${PG_HOST_PORT:-5432}/marketplace_os ALEMBIC_DATABASE_URL=postgresql+psycopg://migrator:ci_only_migrator_not_a_real_password@localhost:${PG_HOST_PORT:-5432}/marketplace_os S3_ENDPOINT_URL=http://localhost:${MINIO_API_HOST_PORT:-9000} PORT=${API_HOST_PORT:-8000} make bootstrap
  docker compose -p "prove-gates-bootstrap-${self_project}" down -v >/dev/null 2>&1
  docker image rm -f "mkos-api:ci-prove-gates-bootstrap-${self_project}" >/dev/null 2>&1
  leg bootstrap-misspelled-bucket bootstrap 'bucket marketplace-csv missing' seed_misspelled_bucket COMPOSE_PROJECT_NAME="prove-gates-bootstrap-${self_project}" DATABASE_URL=postgresql+psycopg://app_rw:ci_only_app_rw_not_a_real_password@localhost:${PG_HOST_PORT:-5432}/marketplace_os ALEMBIC_DATABASE_URL=postgresql+psycopg://migrator:ci_only_migrator_not_a_real_password@localhost:${PG_HOST_PORT:-5432}/marketplace_os S3_ENDPOINT_URL=http://localhost:${MINIO_API_HOST_PORT:-9000} PORT=${API_HOST_PORT:-8000} make bootstrap
  docker compose -p "prove-gates-bootstrap-${self_project}" down -v >/dev/null 2>&1
  docker image rm -f "mkos-api:ci-prove-gates-bootstrap-${self_project}" >/dev/null 2>&1
  leg bootstrap-mimic-no-set-role bootstrap 'permission denied to change default privileges' seed_bootstrap_no_set_role COMPOSE_PROJECT_NAME="prove-gates-bootstrap-${self_project}" DATABASE_URL=postgresql+psycopg://app_rw:ci_only_app_rw_not_a_real_password@localhost:${PG_HOST_PORT:-5432}/marketplace_os ALEMBIC_DATABASE_URL=postgresql+psycopg://migrator:ci_only_migrator_not_a_real_password@localhost:${PG_HOST_PORT:-5432}/marketplace_os S3_ENDPOINT_URL=http://localhost:${MINIO_API_HOST_PORT:-9000} PORT=${API_HOST_PORT:-8000} make bootstrap
  docker compose -p "prove-gates-bootstrap-${self_project}" down -v >/dev/null 2>&1
  docker image rm -f "mkos-api:ci-prove-gates-bootstrap-${self_project}" >/dev/null 2>&1
  docker compose start >/dev/null 2>&1
fi

printf '%s\n' "${summary[@]}" | tee "$logs/summary.txt"
echo "prove-gates: ${#summary[@]} legs, ${failures} not proven (logs in $logs)"
[ "$failures" -eq 0 ]
