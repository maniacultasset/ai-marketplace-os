# AI Marketplace OS

Backend and database for AI Marketplace OS, a listing operator for D2C brands selling on Amazon.in and
Flipkart.

A brand uploads its catalogue once. The system then:
1. drafts each marketplace listing in the brand's voice;
2. validates it against the marketplace's rules;
3. routes it to a person for approval;
4. publishes it through the marketplace APIs.

```
AI drafts  ->  rules validate  ->  a person approves  ->  adapter publishes  ->  performance feeds back
```

AI never publishes. Every listing passes deterministic validation and human approval first.

## Start here

Read these in order. Each is a short summary that points into the detailed sources, which stay the
single source of truth.

1. [`docs/PRD.md`](docs/PRD.md): what v1 must do, for whom, and what it leaves out.
2. [`docs/ARCH.md`](docs/ARCH.md): how the backend is built and where it runs.
3. [`docs/RULES.md`](docs/RULES.md): the engineering rules every contributor follows.
4. [`docs/DESIGN.md`](docs/DESIGN.md): the backend design conventions.
5. [`docs/PHASES.md`](docs/PHASES.md): phases and build status.
6. [`docs/STATE.md`](docs/STATE.md): live status, decisions and next actions.

## Repository

This repository holds the backend and database. Build step S1 created the skeleton: the uv workspace,
a FastAPI app with health probes, the local database stack and the ten required checks.

| Path | Contents |
|---|---|
| `apps/api` | FastAPI service, background workers, marketplace adapters, rules engine |
| `packages/schemas` | API and event contracts: Python `mkos_schemas`, TypeScript `@mkos/schemas` |
| `infra` | OpenTofu for Google Cloud |
| `docs` | Architecture, decisions, specification snapshot, requirements trace |
| `scripts` | `db/` bootstrap SQL, `ci/` check scripts, spec sync, document build, trace checks |

### Related repositories

| Repository | Owner | How it connects |
|---|---|---|
| Frontend (Next.js dashboard) | Frontend team | Calls this API. Installs `@mkos/schemas` from GitHub Packages at a pinned version (publishing starts in S3). |
| AI pipeline (`mkos-pipeline`) | AI team | Runs inside the backend worker through `run_listing_pipeline`. Pinned by git tag; CI uses a stub (the seam and the stub land in S14b). |

Contract changes are released as versions (ADR-0012), so each repository upgrades on purpose.

## Stack

- Python 3.13, FastAPI, Pydantic 2, SQLAlchemy 2 (async) with psycopg 3
- PostgreSQL 17 with pgvector; tenant isolation through tenant-scoped repositories (S9) and row-level
  security (S10), both planned
- Background jobs queued in PostgreSQL (procrastinate)
- uv workspace for Python, pnpm for TypeScript
- Google Cloud, `asia-south1`: Cloud Run, Cloud SQL, Secret Manager, Cloud KMS, Cloud Storage

Each choice and the alternatives considered are recorded in [`docs/adr/`](docs/adr/).

## Quickstart

Prerequisites: Docker Desktop and [uv](https://docs.astral.sh/uv/) 0.12.19 (the pinned version; uv
installs Python 3.13 itself).

```sh
cp .env.example .env
make dev        # Postgres 17 + pgvector, bootstrap SQL, uv sync, git hooks, then the API on :8000
curl -s localhost:8000/healthz && curl -s localhost:8000/readyz
```

- Every port binds `127.0.0.1` only: Postgres on 5432 and the API on 8000. MinIO (9000 and 9001)
  joins the stack once its image source is decided; nothing in the API uses it yet.
- The API docs are at <http://localhost:8000/docs>. Production serves no docs.
- Trailing slashes are never redirected: `/api/v1/x/` is a 404 when the route is `/api/v1/x`, so
  clients must use the exact OpenAPI paths.
- `make test-unit` runs the unit and meta tests without a database.
- Without `make dev` (for example, to run only the checks): `uv sync --frozen --all-packages`, then
  `uv run pre-commit install` before your first commit (it installs the pre-commit and commit-msg
  hooks), and `docker compose up -d --wait` to start Postgres before `make test-db`.

Each required check has a make target of the same name, and CI runs exactly that target:

| Check | Local command | Needs |
|---|---|---|
| `api-lint` | `make api-lint` | network (pip-audit) |
| `api-typecheck` | `make api-typecheck` | — |
| `api-tests` | `make test-db api-tests` | the compose Postgres |
| `tenant-isolation` | `make test-db tenant-isolation` | the compose Postgres |
| `contracts` | `make contracts` | — |
| `schemas-drift` | `make schemas-drift` | — |
| `commit-lint` | `PR_TITLE='feat(x): y' HEAD_REF=feat/x make commit-lint` | — |
| `bootstrap` | `make bootstrap` | Docker, a `.env`, free ports 5432 and 8000 |
| `lint-imports` | `make lint-imports` | — |
| `gitleaks` | `make gitleaks` | network (first run downloads the pinned binary) |

`scripts/ci/prove_gates.sh [--with-docker]` proves that each check goes red on a seeded violation.

## Environments

| Environment | Runs on | Database | Deployed by (planned) | Today |
|---|---|---|---|---|
| Local | Developer machine | PostgreSQL 17 in Docker | — | running |
| Staging | Cloud Run, project `aimos-staging` | Cloud SQL | Every merge to `main` (from S4) | project exists; billing not linked; nothing deployed |
| Production | Cloud Run, project `aimos-prod` | Cloud SQL | An approved release tag `vX.Y.Z`: zero-traffic deploy, smoke test, automatic rollback (from S25) | same as staging |

## Documentation

| Document | Covers |
|---|---|
| [`docs/README.md`](docs/README.md) | Index of every document, and which source wins when two disagree |
| [`docs/EXECUTION-PLAN.md`](docs/EXECUTION-PLAN.md) | Setup, CI/CD, cloud and the roadmap to launch |
| [`docs/architecture/BACKEND-ARCHITECTURE.md`](docs/architecture/BACKEND-ARCHITECTURE.md) | How the system works end to end |
| [`docs/architecture/DATABASE.md`](docs/architecture/DATABASE.md) | Schema, tenant isolation, migrations |
| [`docs/review/TEAM-QUESTIONS.md`](docs/review/TEAM-QUESTIONS.md) | Open questions for the teams, and the default built until each is answered |
| [`docs/source/`](docs/source/) | Snapshot of the client specification |

Word editions of the architecture documents are built with `scripts/docgen/build.sh`. They are not
committed.

## Contributing

- Branch from `main` as `feat|fix|chore|docs|refactor|test|perf/<short-name>`.
- Use conventional commit titles: `type(scope): summary`.
- Open a pull request. All checks must pass, and it is squash-merged after review.
- Changes to `packages/schemas` affect the AI and frontend teams: they need both leads' approval and ship
  as a contract release.
- Never commit secrets. Configuration comes from environment variables, listed in
  [`docs/canon/ENV.md`](docs/canon/ENV.md).

## Status

Build step S1 (scaffold, toolchain and CI checks) is merged. Small follow-ups (local object storage, a
build-tool pin, a pip-audit timeout) are next. Current status and next steps are in
[`docs/STATE.md`](docs/STATE.md).
