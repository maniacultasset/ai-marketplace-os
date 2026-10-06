# mkos-schemas changelog

The Python package `mkos-schemas` and the npm package `@maniacultasset/schemas` (installed by the FE
as `"@mkos/schemas": "npm:@maniacultasset/schemas@<version>"`) share one version, released by one
`schemas-v<version>` tag.

**Version rule after a tag.** Any PR that changes the contract (`jsonschema/**`, `ts/src/**`, the
`mkos_schemas` models, or `openapi.json` through the API registry) once `schemas-v<current>` exists:

- If this file has an open `## 1.N.0 (unreleased)` heading, add the entry under it.
- Otherwise bump to the next MINOR in one commit: `packages/schemas/pyproject.toml`,
  `packages/schemas/ts/package.json`, `SCHEMA_VERSION` in `src/mkos_schemas/base.py`, every fixture's
  `schema_version`, and the regenerated outputs; then open `## 1.N.0 (unreleased)`.
- A description-only change may take a PATCH instead, unless a MINOR heading is already open.
- Classify each change first (docs/RULES.md §11): adding a member to a closed vocabulary (an error
  code to a declared set included) is MAJOR and waits for its window.
- The tagging PR closes the heading (`## 1.N.0 (YYYY-MM-DD)`). The publish workflow refuses a tag
  whose heading is still `(unreleased)`, and `make contracts` refuses contract changes at an
  already tagged version (`scripts/ci/schemas_version_check.sh`).

## 1.0.0

First contract release. It ships outside the 4 Oct window as a one-time exception: nothing is pinned to 0.0.0. Later breaking changes wait for 18 Oct or 1 Nov.

Adding a member to a closed vocabulary (an enum, a `Literal`, a `RuleDef` union, or a path) is a breaking change.

- Requires Python `==3.13.*` and `pydantic>=2.11,<3`.
- `ImportReport` uses `import_id`, not `batch_id`.
- `ErrorCode` has 76 members. The 11 API codes added for the `/api/v1` surface (S3): `UNAUTHENTICATED`, `PERMISSION_DENIED`, `NOT_FOUND`, `METHOD_NOT_ALLOWED`, `IDEMPOTENCY_KEY_REUSED`, `STALE_VERSION`, `PAYLOAD_TOO_LARGE`, `NOT_IMPLEMENTED`, `OAUTH_STATE_INVALID`, `LAST_OWNER_REQUIRED` and `MARKETPLACE_ACCOUNT_CONFLICT`. None has an `ErrorClass`; `NOT_IMPLEMENTED` is `system_failure`, the rest `user_action`.
- Frozen status vocabularies: `ListingStatus` (9), `ListingResult.status` (3), `MarketplaceState` (8), `JobState` (7).
- `ImageRef` refuses `packaging_label` on a publishable listing. `ImageReport` may still name that slot.
- `GenerationContext.product` stays a dict through v1; the `facts.py` retype moves to after v1.
- New generated outputs (S3): `jsonschema/openapi.json` (the frozen `/api/v1` surface: 35 operations at S3, one error envelope, keyset pages) and the TypeScript types `ts/src/{api.d.ts,contracts.d.ts,index.ts}`, published as `@maniacultasset/schemas` (FE alias `@mkos/schemas`).
- Auth and members (S10, additive; still 1.0.0 because `schemas-v1.0.0` is not tagged yet): `POST /api/v1/auth/switch-tenant` (`SwitchTenantRequest {target_tenant_id}`, answers `TokenResponse`), `GET /api/v1/members` (`MemberPage` of `MemberOut`) and `PATCH /api/v1/members/{user_id}` (`MemberPatch {role?, is_active?}`, 409 `LAST_OWNER_REQUIRED`), so 38 operations. Every operation carries `x-mkos-action` (the action it requires, or `public`), and the 403 `PERMISSION_DENIED` responses follow the role x action matrix: `images/upload-url`, `images/upload`, `listings/validate`, `rules/dry-run`, `jobs/{job_id}/cancel` and `brand/documents` now declare it.
- Not in this release: `facts.py`, `batches.py` (the approve and publish batch shapes ship as API models in `openapi.json`), and `CanonicalListing.attribute_sources`.
