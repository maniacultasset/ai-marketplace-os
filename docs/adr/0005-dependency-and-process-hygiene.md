# ADR-0005: Dependency and process hygiene

- **Status:** accepted (user, in chat, 2026-09-27)
- **Date:** 2026-09-27
- **Deciders:** the user (Tech Lead)
- **Evidence:** `docs/research/stack-2026-09.md`

## Decision

1. **Rate limiting:** no slowapi.
   - slowapi silently fails to apply limits to `include_router` routes on FastAPI ≥ 0.137 (#281).
     That covers our whole `/api/v1` surface.
   - Instead, a FastAPI **dependency** backed by an UNLOGGED Postgres fixed-window table:
     `INSERT … ON CONFLICT DO UPDATE … RETURNING n`.
   - Limits: 120/min per tenant, 60/min per IP, 5/min on login.
   - A test asserts a 429 on a route mounted through `include_router`.
2. **Server process:**
   - **One `uvicorn app.main:app` process per container instance.** No gunicorn, and no deprecated
     `uvicorn.workers.UvicornWorker`.
   - Remove `WEB_CONCURRENCY`.
   - Cloud Run (or the host from ADR-0001) scales by adding instances.
3. **Connection ledger, restated:** (processes per instance × instances × pool) summed over the API,
   the worker service(s) and the migration job. It is recomputed in ADR-0001 once the host and its
   instance counts are known. **The Addendum §24 figure (88/200) is wrong:** it's 118/200 with the
   scaffold's 2 gunicorn workers.
4. **Pins and security floors:**
   - `fastapi` pinned exactly; upgrades are deliberate.
   - `sqlalchemy[asyncio]>=2.0.54,<2.1` (2.1 changes the default driver and autoflush).
   - `starlette>=1.3.1`.
   - `pyjwt[crypto]>=2.15`, decoding with exactly `algorithms=["HS256"]`.
   - `pydantic>=2.13.5,<2.14` (2.14 changes JSON Schema output, which would trip schemas-drift).
   - `pydantic-settings>=2.14.2`, `sqladmin>=0.31.1`.
   - One pinned `uv` version in the Dockerfile, in `[tool.uv] required-version` and in CI.
5. **TypeScript codegen:**
   - No `npx --yes` downloads. `openapi-typescript` and `json-schema-to-typescript` become
     exact-pinned devDependencies in the pnpm lockfile.
   - openapi-typescript stopped updates, security included, on 2026-09-19. Migrate `api.d.ts`
     generation to `@hey-api/openapi-ts`, pinned exactly, around launch.
6. **Supply chain:**
   - `pip-audit` (or osv-scanner) runs in CI.
   - Renovate opens weekly grouped PRs.
   - Nothing is auto-merged except dev-dependency patches.

## Consequences

- Rate limits actually fire. The connection budget becomes truthful. Known CVEs stay out. Contract
  generation is reproducible.
- Several Addendum rows change (§3, §13, §24; the Dockerfile and entrypoint). See the CANON.md
  amendments.
