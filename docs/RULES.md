# RULES: engineering rules for every contributor

Purpose: the rules every contributor follows, on any machine and with any editor.

**This is a summary. The sources each section cites remain the single source of truth.** If this
file disagrees with them, they win, ranked by `docs/README.md` "When sources disagree";
fix this file, and any stale source, in the same PR. Live status: `docs/STATE.md`.

## 1. The 10 invariants (never traded for speed)

1. AI never publishes. Only adapters publish, and only after human approval.
2. The Rules Engine is deterministic code, never a model call. It is the final arbiter.
3. Human approval is mandatory, with maker-checker: whoever generated or edited a listing cannot approve it.
4. Marketplace logic stays inside adapters. The canonical listing has no marketplace-specific fields.
5. Every query is scoped by `tenant_id`, enforced at the repository base class, with no exceptions.
6. Marketplace credentials never enter a model prompt, and are KMS-encrypted at rest.
7. Every model call persists an `AgentRun`: model, tokens, cost, duration, status.
8. One `ErrorReport` shape for rules, vision, model and marketplace failures.
9. Self-correction stops after 3 attempts, then the listing is `blocked`. The cap lives in the graph;
   generation jobs use `max_attempts=1` for domain failures.
10. Mutations are idempotent; partial retries touch only the failed field.

Spec page [221] (L13678) lists 14 items; use this numbering.
Source: spec L4692-4707; `BACKEND-ARCHITECTURE.md:174`; ADR-0004 §6.

## 2. Do and avoid

- **Do** read the front door (README "Start here") and then `docs/STATE.md` at every session start;
  before planning a step, pull its rows from `docs/review/`.
- **Do** build in the order of STATE "Start here" item 2 (dependencies from EXECUTION-PLAN
  §6.2–§6.3; a step starts only after its dependencies merge). Step scope and "Done when" for S2–S26
  come from `docs/STEPS.md` (approved 2026-10-04) and are the acceptance tests.
- **Do** cite a file for every claim: `path:line`, a Canon row, or spec `L<line>`.
- **Do** ask instead of guessing: add a TEAM-QUESTIONS row, and build on its default only if waiting
  blocks the step.
- **Avoid** inventing endpoints, columns, env vars, error codes or versions, and copying values from
  `docs/drafts/` or `scaffold/` without checking the Canon and ADRs.
- **Avoid** weakening tenant isolation or a check to pass a test.
- **Avoid** changing cloud resources by hand: reviewed OpenTofu only (S4 staging, S25 prod), except
  the user's by-hand GCP set, imported at S4 (EXECUTION-PLAN decision 5, §5.5).

Source: `README.md` "Start here"; `docs/STATE.md` "Start here" 0 and 2, "GCP by hand";
`EXECUTION-PLAN.md` §5.1, §5.5, §6.2; `TEAM-QUESTIONS.md:1-16`; `SUPERSEDED.md:1-10`; ADR-0009 §4;
`docs/PROCESS.md` §2, §4, §6.

## 3. Canonical names

Register: `CANON.md` "Naming register"; its "Amendments by ADR" section wins. Most often wrong:
`/api/v1` (never `/v1`; unversioned only `/healthz /readyz /openapi.json /docs /internal/admin`),
plural `marketplaces`, `tenant_id` (never `brand_id`; in HTTP only from the JWT `tid` claim; the
one carve-out is `POST /api/v1/auth/switch-tenant`'s `target_tenant_id`, checked against the
caller's own active memberships, ADR-0016),
`APP_ENV` (never `ENV`), `ErrorSource` never `api`. Only the backend worker writes `agent_runs`; a
job timeout is `JOB_TIMEOUT` on a `failed` row.

Source: `CANON.md:5-13`, `:33-57`, `:64`, `:68`, `:113`; `CANON-ADDENDUM.md` §19-§21; ADR-0004 §4, §5;
ADR-0016.

## 4. Libraries

- **Pinned** (full table: `docs/ARCH.md` §8): Python `==3.13.*`; psycopg 3 (`postgresql+psycopg`)
  for app and Alembic; PostgreSQL 17, pgvector >= 0.8.2; `pyjwt[crypto]` HS256 only; one `uvicorn`
  per instance; uv `0.12.19` with `uv.lock` and `uv sync --frozen`.
- **Banned (ruff TID251):** `asyncpg`, `redis`, `arq`, `jose`, `passlib`, `numpy`. `openai` and
  `langgraph` are banned too, except in `app/services/embeddings.py` and `app/services/vision.py`,
  the only backend modules that call a model.
- **Banned, not import-checked:** `slowapi`, `gunicorn`, `cloud-sql-python-connector`, `npx --yes`.
- Add or upgrade a dependency only in an approved plan; Renovate never auto-merges.
- Open: Python 3.13 awaits the AI team (STATE open decision 9); the queue awaits the ADR-0004 spike (T3).

Source: ADR-0002 to ADR-0005; ADR-0013 §3, §6; `pyproject.toml:53-54`, `:56-64`; `CANON.md:126`;
`apps/api/pyproject.toml`; `BACKEND-ARCHITECTURE.md:1275`; `docs/plans/README.md` "Test tooling".

## 5. Configuration

- Only `apps/api/app/core/config.py` reads the environment (repo rule R1). Call `get_settings()`;
  `extra="forbid"` makes an unknown key abort boot.
- `APP_ENV` is always required; `APP_ROLE` (`api|worker|migrate|shell`) sets the other requirements.
- A new setting needs an `ENV.md` row and a matching Settings field. Local values match `docker-compose.yml`.
- Outside `local`, boot refuses a committed or dev-prefixed `JWT_SECRET`.

Source: `apps/api/app/core/config.py:1-10`, `:177-178`; `scripts/ci/check_repo_rules.py`; ADR-0013 §1;
`docs/canon/ENV.md:3-11`.

## 6. API

- Trailing slashes are never redirected: clients use the exact OpenAPI paths.
- Routers -> services -> repositories. Routers never import `sqlalchemy` or `app.db`; services never
  reach the engine (`.importlinter`, repo rule R3).
- Error codes come only from `mkos_schemas/codes.py`, never as literals (repo rule R2); a new code
  needs the Tech Lead's OK first.
- Idempotency: `idempotency_keys` PK `(tenant_id, key)`; `job_runs` `UNIQUE (tenant_id, idempotency_key)`.
  `Idempotency-Key` (1-255 visible ASCII) is required on the S3 D9 set (`openapi.json`); the same key
  and body replay, a different body gets 409 `IDEMPOTENCY_KEY_REUSED`. Another tenant's id answers 404 `NOT_FOUND`, never 403 (S3 D8).
- Rate limits: a Postgres-backed dependency, 120/min per tenant, 60/min per IP, 5/min on login.
- Body cap 10 MiB (`REQUEST_MAX_BODY_BYTES`), enforced before any parsing or auth; `products/bulk` is multipart only in v1 (S3 CP5).
- Publishing happens only through the publish endpoint and worker, after `approved`.
- Another tenant's id is "not found", never "forbidden": `TenantScopedRepository.get_by_id` returns
  `None` for it exactly as for a missing id, so a response never confirms that the row exists.

Source: `BACKEND-ARCHITECTURE.md` §5.3-§5.6; `CANON.md:83`, `:124`; ADR-0005 §1; `README.md` "Quickstart".

## 7. Database

- `tenant_id` on every tenant row; four isolation layers (`docs/ARCH.md` §4); tenant GUCs only via
  `set_config(…, true)`.
- One bypass, `platform_scope()`. `tenant_scope_checked` appears only in `app/db/guard.py` and
  `app/db/session.py` (checked by `make lint-imports`).
- Status columns are TEXT + CHECK, never a Postgres ENUM.
- Revision ids are claimed in the Canon ledger, with the Tech Lead's OK, before the file exists.
  `scripts/db/0000_bootstrap.sql` is raw SQL; Alembic runs as `migrator` before deploy.
- Additive only (expand/contract): no rename, type change or narrowed CHECK in one release;
  `CONCURRENTLY`; `NOT VALID` then `VALIDATE`; `SET lock_timeout = '5s'`; squawk. No prod downgrade.
- A job is enqueued in the transaction that writes its rows; the payload holds only `job_run_id`.
- Inserts into the 7 `request_id` tables go through the ORM or a Core `Table`, never `text()` SQL;
  `audit()` is the only SQL-side writer (`tests/meta/test_raw_sql_inserts.py`).
- Dev and every test use the compose PostgreSQL (`127.0.0.1:5432`); Cloud SQL is for staging and
  prod only, so never point tests or `alembic downgrade` at it.
- Open: the schema source for S5 onward (STATE open decision 10).

Source: `DATABASE.md` §4, §8.2, §8.3; `CANON.md:52`, `:75-76`, `:86-98`; `CANON-ADDENDUM.md` §6;
ADR-0004 §2, §4; `EXECUTION-PLAN.md` §5.3; `docker-compose.yml:13`.

## 8. Testing

- Write the failing test from "Done when" first, and prove every guard can fail (drop the tenant
  filter: isolation goes red; `scripts/ci/prove_gates.sh`).
- "Done" means every required check passed, with fresh output; name any skip. Docs-only: "docs-only,
  no gates run".
- Each of the 10 checks has a same-named `make` target (README "Quickstart"). Run `make test-db`,
  with the compose Postgres up, before `api-tests` and `tenant-isolation`.
- Some checks run reduced until their step (ADR-0013 §4); an empty test selection fails.
- Coverage: 70 from Day 11, +5 each Friday (`COV_FAIL_UNDER`). Core modules (rules engine, tenant
  guard, maker-checker, idempotency, publish retry): 90% branch and nightly mutation score >= 80%.
- No network in unit tests (`--disable-socket`). Stand-ins: a fake marketplace server and
  `FakeAdapter`, `LocalKmsStub`, MinIO, the stub pipeline. Never real credentials or real
  marketplace, KMS or model APIs.
- ADR-0010 gates await the team (T8). MinIO image source is T107 (Chainguard compose, `/data`).

Source: `README.md` "Quickstart"; `Makefile`; `CANON.md:79`, `:128`, `:136-137`; ADR-0010;
ADR-0012 §3; `pyproject.toml:80`; `docs/STATE.md` "Start here" 2.8; `docs/PROCESS.md` §4.

## 9. Security

- Never commit secrets: `.env` is ignored; gitleaks runs before commit (hooks: §10), on each PR and
  on `main`.
- Deployed secrets come from Secret Manager as env vars. No key files; CI deploys through WIF.
- The pipeline gets a pushed context and can't import credential modules. Seller tokens are
  envelope-encrypted and decrypted only in the adapter, in memory.
- Least privilege: the app runs as `app_rw` (no superuser, no BYPASSRLS), migrations as `migrator`;
  cloud accounts get only their listed roles (open: KMS grants, EXECUTION-PLAN decision 15).
- Logs and errors never hold secret values or personal data. Local ports bind `127.0.0.1`.
- Workflows: SHA-pinned actions, `permissions: {}`, no `pull_request_target`, PR text via `env:`.
- Project code and docs never go to third-party services; only pip-audit and Renovate send package
  names and versions.

Source: `.pre-commit-config.yaml`; `.github/workflows/`; `BACKEND-ARCHITECTURE.md` §9.4-§9.5;
`CANON.md:72`, `:125`; `EXECUTION-PLAN.md` §4.4, §4.6; ADR-0013 §5-§6; `DATABASE.md:129`.

## 10. Git and pull requests

- Branch `^(feat|fix|chore|docs|refactor|test|perf)/[a-z0-9._-]+$`; PR title (the squash commit)
  `^(feat|fix|chore|docs|refactor|test|perf)(\([a-z0-9-]+\))?: .+`. A commit-msg hook checks commits.
- Install the hooks (`make hooks`, or `uv run --frozen pre-commit install`; `make dev` calls `hooks`) before your first commit.
- Short branch -> PR -> all 10 checks green (advisory on GitHub Free, so confirm them) -> review ->
  squash merge by the Tech Lead. No direct or force pushes to `main`.
- The PR body follows `.github/pull_request_template.md` with scope, criteria and results.
- No tool attribution: no co-author trailers naming an AI tool and no "generated with" footers.
  Commits, PRs and files are plain engineering prose. Never force-add paths in `.git/info/exclude`.
- The Tech Lead approves each plan before code and each merge. ADR numbers are claimed in
  `docs/adr/README.md`; ADRs start `proposed`, and only the Tech Lead accepts one.
- Prod deploys only an approved `vX.Y.Z` tag; rollback redeploys the previous tag.

Source: `CANON.md:142`; `docs/STATE.md` header and "Decided" (S1 plan decisions; no AI branding);
`.pre-commit-config.yaml:1-3`; `Makefile` `dev`; ADR-0013 §5; ADR-0009; `docs/adr/README.md:1-5`;
`EXECUTION-PLAN.md` §2 decision 3; `docs/PROCESS.md` §5, §7.

## 11. Contracts (`packages/schemas`)

- Every change needs the Tech Lead, AI lead and FE lead; an AI-seam change is also a `CANON.md` PR.
  **Interim (STATE P8, 2026-09-30):** until the AI and FE leads are named, the Tech Lead approves
  alone. Every release ships a changelog entry and a notice to both teams, which adopt pinned versions
  when ready.
- MINOR (additive optional field) any day. MAJOR (rename, removal, type change) only on Day 14
  (Sun 4 Oct), Day 28 (Sun 18 Oct) or Day 42 (Sun 1 Nov).
- Adding a member to a closed vocabulary (an enum, a `Literal`, a `RuleDef` union or a path
  pattern) is MAJOR: pinned consumers reject an unknown value (user, 2026-10-05, S2 Decision 3;
  overrides "widening is MINOR" at `DATABASE.md:1195`).
- Generated `openapi.json` and `.d.ts` files are machine-owned: `make -C packages/schemas all`.
- Versioned releases: `@mkos/schemas` on GitHub Packages; Python pins `mkos-schemas` by git tag.

Source: `CANON.md:45`, `:130`; ADR-0012 §2, §5; `EXECUTION-PLAN.md` §2 decision 9, §6.4.

## 12. Keeping the docs current

A PR that changes behaviour, structure, scope or status also updates the front-door file and the
detailed source behind it:
- scope: `PRD.md`, plus `review/REQUIREMENTS-TRACE.md` or `TEAM-QUESTIONS.md`;
- structure: `ARCH.md`, plus `architecture/BACKEND-ARCHITECTURE.md` or `DATABASE.md`;
- design conventions: `DESIGN.md`, plus a new ADR and CANON "Amendments by ADR" when a row changes;
- phases and roadmap: `PHASES.md`, plus `EXECUTION-PLAN.md` §6;
- status and next actions: `STATE.md`; env vars: `canon/ENV.md`; rules: this file (all under `docs/`).

Rebuild Word editions from the `.md` with `scripts/docgen/build.sh`; never edit them alone.

Source: `docs/STATE.md` "Decided" (front-door docs, user 2026-09-30); `docs/README.md`;
`docs/adr/README.md:3-5`.
