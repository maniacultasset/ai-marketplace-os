# Onboarding: AI Marketplace OS backend

This repository is the backend and database of AI Marketplace OS: the `/api/v1` API, the schema and
migrations, the job workers, the marketplace adapters, the Rules Engine, auth and roles, and the
contracts package `packages/schemas`. The AI pipeline and the web app live in their own repositories.
Start with [README.md](README.md), then read this page top to bottom.

## The 10 invariants

Never traded for speed. Full text and sources: [docs/RULES.md §1](docs/RULES.md).

1. AI never publishes; only adapters publish, after human approval.
2. The Rules Engine is deterministic code, never a model call, and the final arbiter.
3. Human approval with maker-checker: whoever generated or edited a listing cannot approve it.
4. Marketplace logic stays in adapters; the canonical listing has no marketplace-specific fields.
5. Every query is scoped by `tenant_id`, enforced at the repository base class.
6. Marketplace credentials never enter a prompt, and are KMS-encrypted at rest.
7. Every AI call persists an `AgentRun`.
8. One `ErrorReport` shape for every failure source.
9. Self-correction stops after 3 attempts, then `blocked`; no infinite loops.
10. Mutations are idempotent; partial retries touch only the failed field.

## Who owns what on the AI seam

- Ownership line: BACKEND owns brand-document chunking, embedding calls, the deterministic Pillow image checks and BrandChunkRepository.search. AI owns all generation and correction prompts plus the vision prompt content, exposed to them through deps.validate_images. This line is repeated verbatim in ONBOARDING.md.

The seam is one call, `run_listing_pipeline(ctx, deps)`, with five backend ports
(`retrieve_brand_chunks`, `validate_rules`, `validate_images`, `get_model_credentials`,
`emit_node_event`). The backend side is `apps/api/app/services/pipeline_deps.py`.

**The stub.** This repository ships a deterministic stand-in for the AI team's pipeline:
`packages/pipeline-stub` (import `mkos_pipeline`). It returns a valid result from fixtures with no
model, no credential and no network; a `stub:<name>` prefix on `user_instructions` picks a
scenario (`ok`, `blocked`, `failed`, `vision_warn`). PR and nightly jobs only ever use the stub. The
deploy build installs the AI team's tagged `mkos-pipeline` in its place (wired at S4 and S18), and a
production worker refuses to boot with the stub installed.

## A 10-minute architecture read

1. [docs/PRD.md](docs/PRD.md): what v1 must do, for whom, and what it leaves out.
2. [docs/ARCH.md](docs/ARCH.md): the request path, the job path, the AI seam, tenant isolation.
3. [docs/DESIGN.md](docs/DESIGN.md): layers, API, data, jobs, the listing lifecycle, adapters.
4. [docs/RULES.md](docs/RULES.md): the engineering rules every change follows.
5. [docs/canon/CANON.md](docs/canon/CANON.md): the canonical names and decisions.

## Repository tour

- `apps/api/app/api/`: the `/api/v1` routers; `surface.py` is the one registry of operations.
- `apps/api/app/services/`: business logic, including the Rules Engine (`rules_engine/`), the rules
  loader and the seam ports.
- `apps/api/app/repositories/`: every query, scoped by tenant at the base class.
- `apps/api/app/db/`: models, sessions, the tenant guard and the seed.
- `apps/api/app/workers/`: the procrastinate job spine.
- `apps/api/app/adapters/`: the `MarketplaceAdapter` interface and the in-memory `FakeAdapter`.
- `apps/api/app/admin/`: the read-only staff admin at `/internal/admin` (local only in v1).
- `apps/api/alembic/versions/`: the migrations.
- `packages/schemas/`: the contracts shared with the AI and web teams (Python and TypeScript).
- `packages/pipeline-stub/`: the stub pipeline described above.
- `scripts/ci/`: the check scripts; `scripts/e2e/`: the local HTTP end-to-end scripts.

## How to run

You need Docker, `uv` and Python 3.13. Copy `.env.example` to `.env` first. Then start the stack and
the API (the first command keeps serving; leave it running in its own terminal):

```sh
make dev  # background: wait /healthz
make seed
make e2e
```

`make seed` loads the demo tenants (`acme`, `bharat`; every user's password is the local-only
`devpassword123`). `make e2e` runs every script in `scripts/e2e/` against your server. With
`make dev` running, the staff admin is at `/internal/admin`.

### The required checks

Every pull request must pass ten checks; each is a make target with the check's name. These run
locally:

```sh
make api-lint
make api-typecheck
make lint-imports
make test-db
make api-tests
make tenant-isolation
make contracts
make schemas-drift  # skip: installs the pinned TypeScript toolchain from the npm registry; CI runs it on every PR
```

These need the CI environment or CI-only tools, so they are documented here and run by CI:

```text
make bootstrap      # builds the image and runs it against its own compose stack
make commit-lint    # needs PR_TITLE and HEAD_REF from the pull request
make gitleaks       # needs the pinned gitleaks binary
```

## How work flows

- Every step goes through the team loop in [docs/PROCESS.md](docs/PROCESS.md) §4: a plan, a code
  plan, reviews, test-first implementation, an attack pass, review, then a pull request the Tech
  Lead merges.
- Branches and pull requests follow [docs/RULES.md](docs/RULES.md) §10.
- Architecture decisions are ADRs: see [docs/adr/README.md](docs/adr/README.md) for the template and
  how a number is claimed. A new ADR is always proposed; only the Tech Lead accepts it.

## GCP access

Ask the Tech Lead. Nobody provisions cloud resources by hand; staging and production change only
through reviewed OpenTofu.
