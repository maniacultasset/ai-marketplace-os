# ADR-0013: Toolchain, boot settings and CI for the backend-only repo on GitHub Free

- **Status:** accepted (user, in chat, 2026-09-29). Item 5 departs from
  the site's Day-1 branch protection (L4882, L4885) until GitHub Team lands.
- **Date:** 2026-09-29
- **Deciders:** the user (Tech Lead)

## Context

- S1 builds the scaffold, the toolchain and the required checks (`docs/drafts/BUILD-SEQUENCE.md:6-20`).
  Three facts have changed since the Canon and Addendum rulings were written:
  - The repo is backend-only (ADR-0012), so the checks drop from 12 to 10.
  - The queue has no Redis (ADR-0004), so the CI env block is 3 variables (CANON.md:21).
  - The org stays on GitHub Free for now (STATE.md:134-135). Free has no rulesets, required checks,
    CODEOWNERS enforcement or environments on private repos.
- Some Addendum rulings can't be met as written in S1:
  - They assume later steps' artifacts: Alembic, the seed and `mkos_pipeline` in the `bootstrap` job
    (CANON-ADDENDUM.md:175-182).
  - They require `OPENAI_API_KEY`, `SENTRY_DSN` and `KMS_KEY_URI` before any feature uses them
    (`scaffold/apps/api/app/core/config.py:100-104`; EXECUTION-PLAN.md:60).
- Some rulings assume a host `psql` that this Mac lacks (CANON-ADDENDUM.md:55-60; STATE.md:268).
- ruff never enforces the banned-API rule, because `TID` is not selected (KNOWN-ISSUES.md:35).

## Decision

1. **Boot requirements by role** (EXECUTION-PLAN decision 7).
   - Settings reads `APP_ROLE` (`api|worker|migrate|shell`, default `api`); `APP_ROLE` becomes a new
     ENV.md row.
   - `APP_ENV` is always required.
   - `api` also requires `DATABASE_URL` and `JWT_SECRET`, and `worker` requires `DATABASE_URL`.
   - `migrate` requires the Alembic URL (ENV.md:36).
   - Every other deployed-only requirement is added by the step whose feature reads it (KMS at S15,
     OpenAI at S16/S18, Sentry at S11, the admin-secret guard at S14).
   - The CI env block stays the three variables of CANON.md:21.
   - Amends CANON-ADDENDUM.md:196 ("four hard-required") and :221-225 until S14.
2. **No FE-only keys.** `next_public_api_base_url`, `nextauth_url` and `nextauth_secret` leave Settings
   and `.env.example`. They existed only so a shared root `.env` validated (CANON-ADDENDUM.md:213-220),
   and ADR-0012 removed `apps/web`. Amends A§8 and ENV.md's three FE rows.
3. **Lint adds `TID` plus two repo checks in `api-lint`:**
   - The banned APIs are `asyncpg`, `redis` and `arq` (ADR-0004); `jose`, `passlib` and `numpy`
     (`docs/RULES.md` §4); and `openai`/`langgraph` outside their Canon exemptions (CANON.md:126).
   - No `os.environ`/`os.getenv` outside `app/core/config.py`.
   - No string literal passed as `error_code=`.
   - `scripts/docgen/**` and `scripts/trace/**` (doc tooling on system Python 3.9) are out of lint
     scope.
   - Amends CANON.md:134.
4. **Required checks are staged.** All 10 run on every PR from S1 onwards. Until the step that gives a
   check its content, it runs a reduced form, and each reduction is removed by that step:
   - `api-tests`: the psql bootstrap step and pytest. The Alembic round trip and squawk join in S5.
   - `api-tests`: `pytest -q -m "not tenant_isolation"`, because the sentinel asserts `mos_iso`; S9
     removes the filter with the sentinel.
   - `tenant-isolation`: one sentinel test proving the marker, the job and the `mos_iso` database.
     The real tests land in S9.
   - `contracts`: a bare-venv install and `import mkos_schemas`. Fixtures come in S2, oasdiff in S3.
   - `schemas-drift`: `make -C packages/schemas all` with no generators yet, then the `git diff`.
     The generators come in S2/S3.
   - `bootstrap`: the A§7 job without alembic (S5) or seed (S9). The image import omits
     `mkos_pipeline` until S14.
   - `lint-imports`: contracts only over packages that exist.
   - An empty test selection (pytest exit 5) is always a failure.
   - Amends CANON.md:136 and CANON-ADDENDUM.md:175-182 and :276 for the interim.
5. **Free-plan operating mode** (EXECUTION-PLAN.md §4.3):
   - Checks are advisory. The user merges only when all 10 are green.
   - No GitHub secrets. This does not withdraw ADR-0012:48-49 (CI read access to the AI repository).
     PR and nightly jobs never fetch `mkos-pipeline`; they use the stub (EXECUTION-PLAN.md:807). Only
     the deploy build fetches the pinned pipeline, with a read-only credential kept in GCP Secret
     Manager and reached through WIF, which binds only `main` (EXECUTION-PLAN.md:303-311), never a
     GitHub secret. The mechanism is settled in S4/S14 (T104).
   - CODEOWNERS is committed but inert.
   - "A force-push to main is rejected" and the §4.2 rulesets move to "when Team lands".
6. **Dependencies:**
   - Renovate is configured self-hosted (`renovate.json`), with **no auto-merge** of any kind. It runs
     from the laptop while on Free. Amends ADR-0005:42.
   - `pip-audit` in `api-lint` sends package names and versions to the advisory service. That is
     recorded as the exception in `docs/PROCESS.md` §1.
7. **Local bootstrap without host psql.** `make bootstrap-db` runs `psql` inside the compose `postgres`
   container, against the file already mounted at `/docker-entrypoint-initdb.d/0000_bootstrap.sql`
   (CANON-ADDENDUM.md:20). CI keeps the host `psql` step (CANON-ADDENDUM.md:63-70). Amends
   CANON-ADDENDUM.md:55-60.

## Consequences

- S1 is buildable today and honest about what each check proves. Every reduced check is listed, with
  the step that restores it.
- On acceptance, record it in CANON.md "Amendments by ADR". Also:
  - add the `APP_ROLE` row to ENV.md and remove the three FE rows;
  - add TEAM-QUESTIONS rows for item 1 (decision 7) and item 5 (site L4882/L4885).
- The risk is that a reduced check gets forgotten. Each later step's plan must list the reductions
  it removes, and S9 must turn `tenant-isolation` into the real mapper-looping suite (CANON.md:137).
