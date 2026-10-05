# ADR-0012: This repository holds the backend only

- **Status:** accepted (user, in chat, 2026-09-29). **Provisional against the site:** the client spec
  scaffolds one monorepo with `apps/web` (L4881, L6964), so the teams confirm it in TEAM-QUESTIONS T103.
  Where the AI pipeline lives (item 3) is a recommended default until the AI team confirms (T104).
- **Date:** 2026-09-29
- **Deciders:** the user (Tech Lead)

## Context

- The Canon planned one repository for all three teams: `apps/api`, `apps/web`, `packages/schemas`,
  `packages/pipeline`, `infra` (CANON.md:84).
- The frontend is built by a separate team in its own repository. The AI team may keep its own
  repository too.
- Our team owns the backend and database end to end: schema, APIs, workers, adapters, the rules
  engine, security and cloud infrastructure.

## Decision

1. **`maniacultasset/ai-marketplace-os` is backend-only.** It holds `apps/api`, `packages/schemas`,
   `infra`, `scripts`, `docs` and `.github`. There is no `apps/web`.
2. **Contracts are published, not shared through a workspace:**
   - **TypeScript:** `@mkos/schemas` is generated from `packages/schemas` (the OpenAPI document and the
     JSON Schemas) and published to GitHub Packages (npm) in the `maniacultasset` org on each contract
     release tag. The frontend repository installs a pinned version.
   - **Python:** other repositories depend on `mkos-schemas` through a git tag of this repository
     (`packages/schemas` subdirectory), pinned in their lockfile.
3. **AI pipeline (recommended default, T104):**
   - The AI team's `mkos-pipeline` lives in their own repository.
   - The backend worker installs it at a pinned git tag, recorded in `uv.lock`.
   - The seam is unchanged: `run_listing_pipeline(ctx, deps) -> GenerationResult` with five ports; the
     AI code opens no database session (invariants 1, 6 and 7).
   - This repository keeps a **stub pipeline** that implements the seam deterministically, so our tests
     and CI never depend on the AI team's release timing.
4. **Required checks drop from 12 to 10:** `web-lint` and `web-typecheck` move to the frontend
   repository.
5. **Cross-team review:**
   - Changes to `packages/schemas` still need the AI and frontend leads' approval (`docs/RULES.md` §11).
   - They land as versioned contract releases, MINOR any day and MAJOR only in the Day-14/28/42
     windows.

## Consequences

- Each team releases independently. A contract change reaches the other teams only when they bump the
  pinned version, so drift is visible and deliberate.
- Integration is proven by the stub in our CI, and by a contract test the AI team runs against a
  tagged `mkos-schemas`.
- CI needs read access to the AI repository to install the pinned pipeline, through a read-only
  deploy key or a GitHub App token.
- These Canon rows are amended: CANON.md:41 (TypeScript package consumption), :46 (AI package home and
  workspace membership), :84 (repository layout) and the 12-check list (Addendum §12).

## Amendment 1 (S3, 2026-10-05): the published npm name

- **Status:** proposed (the user sets `accepted`). Approval to write it: Tech Lead and FE lead,
  2026-10-05 (S3 CP1).
- **Context:** GitHub Packages (npm) accepts only a scope equal to the owning account, so
  `@mkos/schemas` under the `maniacultasset` org would be refused on publish day.
- **Decision (amends item 2, TypeScript):** the package is published as `@maniacultasset/schemas`,
  from `packages/schemas/ts`, at the `mkos-schemas` version, by `.github/workflows/publish-schemas.yml`
  on a `schemas-v<version>` tag. The frontend installs it under the canonical name with an npm alias,
  `"@mkos/schemas": "npm:@maniacultasset/schemas@<version>"`, so every import still reads
  `@mkos/schemas` (CANON.md:41 note).
- **Consequences:** the FE's `.npmrc` maps the `@maniacultasset` scope to `https://npm.pkg.github.com`
  with a read token. Publishing to a registry that accepts `@mkos` would need a further amendment.
