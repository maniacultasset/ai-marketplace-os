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

## Repository

This repository holds the backend and database. The application paths are created in build step S1;
today the repository holds the design, the decisions and the tooling.

| Path | Contents |
|---|---|
| `apps/api` | FastAPI service, background workers, marketplace adapters, rules engine |
| `packages/schemas` | API and event contracts: Python `mkos_schemas`, TypeScript `@mkos/schemas` |
| `infra` | OpenTofu for Google Cloud |
| `docs` | Architecture, decisions, specification snapshot, requirements trace |
| `scripts` | Spec sync, document build, trace checks |

### Related repositories

| Repository | Owner | How it connects |
|---|---|---|
| Frontend (Next.js dashboard) | Frontend team | Calls this API. Installs `@mkos/schemas` from GitHub Packages at a pinned version. |
| AI pipeline (`mkos-pipeline`) | AI team | Runs inside the backend worker through `run_listing_pipeline`. Pinned by git tag; CI uses a stub. |

Contract changes are released as versions (ADR-0012), so each repository upgrades on purpose.

## Stack

- Python 3.13, FastAPI, Pydantic 2, SQLAlchemy 2 (async) with psycopg 3
- PostgreSQL 17 with pgvector; tenant isolation enforced with row-level security
- Background jobs queued in PostgreSQL (procrastinate)
- uv workspace for Python, pnpm for TypeScript
- Google Cloud, `asia-south1`: Cloud Run, Cloud SQL, Secret Manager, Cloud KMS, Cloud Storage

Each choice and the alternatives considered are recorded in [`docs/adr/`](docs/adr/).

## Environments

| Environment | Runs on | Database | Deployed by |
|---|---|---|---|
| Local | Developer machine | PostgreSQL 17 in Docker | — |
| Staging | Cloud Run, project `aimos-staging` | Cloud SQL | Every merge to `main` |
| Production | Cloud Run, project `aimos-prod` | Cloud SQL | An approved release tag `vX.Y.Z`: zero-traffic deploy, smoke test, automatic rollback |

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

Design complete; implementation starts with build step S1 (September 2026). Current status and next steps
are in [`docs/STATE.md`](docs/STATE.md).
