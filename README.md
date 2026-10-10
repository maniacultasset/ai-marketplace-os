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
6. [`docs/PROCESS.md`](docs/PROCESS.md): how the team plans, builds, reviews and merges.
7. [`docs/STATE.md`](docs/STATE.md): live status, decisions and next actions.

## Repository

This repository holds the backend and database. Build step S1 created the skeleton: the uv workspace,
a FastAPI app with health probes, the local database stack and the required checks (ten at S1; eleven since S26 split `api-tests` in two).

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
installs Python 3.13 itself). Regenerating the contract types (`make -C packages/schemas all`,
`make schemas-drift`) also needs Node 24 and pnpm (`corepack enable pnpm` picks the pinned version).

```sh
cp .env.example .env
make hooks      # or: uv run --frozen pre-commit install
make dev        # Postgres, MinIO, bootstrap SQL, uv sync, hooks, then the API on :8000 (the default)
curl -s localhost:8000/healthz && curl -s localhost:8000/readyz
```

- Every port binds `127.0.0.1` only. The default host ports are Postgres 5432, the API 8000, MinIO
  API 9000 and console 9001; a second worktree changes them (below). `minio-init` creates
  `marketplace-images` and `marketplace-csv`. Nothing in the API uses MinIO yet.
- The API docs are at <http://localhost:8000/docs>. Production serves no docs.
- Trailing slashes are never redirected: `/api/v1/x/` is a 404 when the route is `/api/v1/x`, so
  clients must use the exact OpenAPI paths.
- `make test-unit` runs the unit and meta tests without a database.

The `/api/v1` contract is frozen as `packages/schemas/jsonschema/openapi.json`; until each endpoint's
owner step lands, the API answers 501 `NOT_IMPLEMENTED` in the error envelope. To work against the
contract without a database, run the local mock, which serves the committed examples on loopback:

```sh
make mock                      # 127.0.0.1:4010 (MOCK_HOST_PORT); `make mock-smoke` probes it
curl -s localhost:4010/api/v1/products
curl -s -H 'Prefer: code=404' localhost:4010/api/v1/products/0b1c2d3e-0000-4000-8000-000000000101
make -C packages/schemas all   # regenerate jsonschema/, openapi.json and ts/src (never hand-edit)
```

`Prefer: code=<status>` answers that status's declared example; a status the operation does not
declare is a 422. The FE installs the types as
`"@mkos/schemas": "npm:@maniacultasset/schemas@<version>"` from GitHub Packages.
- Without `make dev` (for example, to run only the checks): `cp .env.example .env`, then
  `make hooks` (or `uv run --frozen pre-commit install`) before your first commit (it installs the
  pre-commit and commit-msg hooks), and `docker compose up -d --wait postgres` to start Postgres
  before `make test-db`. Waiting on every service fails on Compose v5.5.1, because `minio-init`
  exits 0. Every `docker compose` call needs `.env` under the `:?` form.

Each required check has a make target of the same name, and CI runs exactly that target:

| Check | Local command | Needs |
|---|---|---|
| `api-lint` | `make api-lint` | network (pip-audit) |
| `api-typecheck` | `make api-typecheck` | — |
| `api-tests` | `make test-db api-tests` | the compose Postgres and MinIO (shard 1 of the api suite) |
| `api-tests-integration` | `make test-db api-tests-integration` | the compose Postgres and MinIO (shard 2) |
| `tenant-isolation` | `make test-db tenant-isolation` | the compose Postgres |
| `contracts` | `make contracts` | Docker (oasdiff) once the base branch has `openapi.json` |
| `schemas-drift` | `make schemas-drift` | Node 24 + pnpm; network on the first run (pinned devDependencies) |
| `commit-lint` | `PR_TITLE='feat(x): y' HEAD_REF=feat/x make commit-lint` | — |
| `bootstrap` | `make bootstrap` | Docker, a `.env`, free host ports (defaults 5432, 8000, 9000, 9001) |
| `lint-imports` | `make lint-imports` | — |
| `gitleaks` | `make gitleaks` | network (first run downloads the pinned binary) |

`scripts/ci/prove_gates.sh [--with-docker]` proves that each check goes red on a seeded violation.

### A second stack in a git worktree

The compose project is the folder name, which is already unique. `.worktree.env` is git-ignored
and read only by make and the scripts, never by the app (Settings rejects unknown `.env` keys).
`JWT_ISSUER` and the OAuth redirect URIs stay on 8000: they are exact registered strings
(`docs/canon/ENV.md`). `make dev`, `make bootstrap` and the test targets stop if `.env` or the
running stack disagrees with `.worktree.env`.

```sh
git worktree add ../ai-marketplace-os-trackb -b <branch> origin/main
cd ../ai-marketplace-os-trackb
printf '%s\n' PG_HOST_PORT=5433 MINIO_API_HOST_PORT=9010 MINIO_CONSOLE_HOST_PORT=9011 \
  API_HOST_PORT=8001 MOCK_HOST_PORT=4011 > .worktree.env
cp .env.example .env   # then change only the ports: DATABASE_URL and ALEMBIC_DATABASE_URL
                       # -> :5433, S3_ENDPOINT_URL -> :9010, PORT -> 8001
make dev
```

Plain compose in that folder loads the same file in a subshell:

```sh
(e="$(scripts/ci/worktree_env.sh exports)" && eval "$e" && docker compose ps)
```

Tear the second stack down with `docker compose down -v`, then `cd -` and
`git worktree remove ../ai-marketplace-os-trackb`. Remove old `mkos-api:ci*` images with
`docker image rm`.

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
