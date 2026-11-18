# ADR-0033: Product type definitions through an allow-listed seller connection

- **Status:** accepted (owner authorised using the owner's own brand connection, 18 Nov 2026).
  Amends DW2 decision 4 (T53, `docs/drafts/DECISIONS.md`): the platform-owned authorization is no
  longer the only way to fetch a definition.
- **Date:** 2026-11-18
- **Deciders:** the owner (client), relayed by the Tech Lead

## Context

- DW2 fetches Amazon Product Type Definitions (PTD) only with `templates refresh` under
  `AMAZON_PLATFORM_REFRESH_TOKEN`, a self-authorization of the Kuinn SP-API app. That secret is
  still empty in production, so no definition is stored for SHIRT, KURTA, PANTS, SHORTS or SKIRT.
- Without a stored definition the pre-send schema check is skipped (`ptd.missing`). A beta seller's
  T-shirt went to Amazon without `style` and Amazon rejected it.
- The owner's own brand (tenant slug `test`) holds an active Amazon.in connection authorised through
  the same Kuinn app. `getDefinitionsProductType` is a read that returns the same definition for
  any authorised seller in the marketplace, and the owner agreed to use that connection for it.

## Decision

1. `templates refresh --product-type T --staff-email E --reason R --via-tenant SLUG [--dry-run
   [--save-schema PATH]]` fetches the definition under tenant SLUG's stored Amazon connection
   instead of the platform token. Storage, versions, `promote-definition`, `diff` and `check` are
   unchanged.
2. Guards, all before a token is opened:
   - SLUG must be on the new setting `AMAZON_PTD_TENANT_SLUGS`. It is empty by default, which
     refuses every tenant. `prod.env` sets `test`; staging leaves it blank.
   - The staff email must be an active platform admin, the same check the proposal review uses.
   - The tenant's Amazon account must be `active` and hold a stored token.
3. The CLI never imports the decrypt path (import contract), and workers write no SQL (R4).
   - The CLI checks the allow-list and the admin, resolves the slug, and runs
     `python -m app.workers.ptd_tenant` as a child process.
   - The child checks the allow-list again and loads the account through `tenant_uow`, writing one
     audit row `marketplace_account.ptd_fetch` that names the staff email.
   - The child opens the token blob (KMS), does one LWA refresh in memory as `amazon-check` does,
     and calls only `getDefinitionsProductType` plus the pre-signed schema download. Both go
     through `GetOnlyTransport`, so nothing but a GET can leave.
   - The child hands the fetched definition to the CLI in a 0600 file inside a private temporary
     directory. The CLI recomputes the digest and stores it as `migrator`. The
     `product_type_definition.stored` audit reason ends with `(via tenant SLUG)`. The audit
     allow-list keeps only the key fields, so the tenant goes in the reason.
4. Deploy: `deploy.sh prod templates refresh ... --via-tenant SLUG` runs its own job,
   `aimos-ptd-tenant`, under the PTD identity `aimos-ptd`.
   - It mounts `DATABASE_URL`, `ALEMBIC_DATABASE_URL` and `AMAZON_CLIENT_SECRET`.
   - It never mounts, grants or asks for `AMAZON_PLATFORM_REFRESH_TOKEN`.
   - `aimos-ptd` gets decrypt only (`roles/cloudkms.cryptoKeyDecrypter`) on the token key, and
     access to `DATABASE_URL`.
   - The worker identity is not used: giving the always-on worker pool the migrator URL would widen
     it far more than giving decrypt to an identity that only one-off staff jobs run as.

## Consequences

- Production can store the five apparel definitions today, with no new secret and no owner
  console step. The platform-token path stays as it is for when the self-authorization exists.
- The calls count against the owner's seller quota (one GET per run) and depend on that connection
  staying authorised. A revoked or expired connection fails closed with a clear line.
- `aimos-ptd` can now decrypt tenant tokens. The code limits it to allow-listed tenants, and the
  identity runs only these one-off jobs. ADR-0018's api/worker split for the token key is otherwise
  unchanged, and `setup` still binds only the api and the worker.
- Adding another tenant to the allow-list is an env-file change plus the owner's OK for that
  account.
