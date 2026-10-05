# mkos-schemas changelog

## 1.0.0

First contract release. It ships outside the 4 Oct window as a one-time exception: nothing is pinned to 0.0.0. Later breaking changes wait for 18 Oct or 1 Nov.

Adding a member to a closed vocabulary (an enum, a `Literal`, a `RuleDef` union, or a path) is a breaking change.

- Requires Python `==3.13.*` and `pydantic>=2.11,<3`.
- `ImportReport` uses `import_id`, not `batch_id`.
- `ErrorCode` has 76 members. The 11 API codes added for the `/api/v1` surface (S3): `UNAUTHENTICATED`, `PERMISSION_DENIED`, `NOT_FOUND`, `METHOD_NOT_ALLOWED`, `IDEMPOTENCY_KEY_REUSED`, `STALE_VERSION`, `PAYLOAD_TOO_LARGE`, `NOT_IMPLEMENTED`, `OAUTH_STATE_INVALID`, `LAST_OWNER_REQUIRED` and `MARKETPLACE_ACCOUNT_CONFLICT`. None has an `ErrorClass`; `NOT_IMPLEMENTED` is `system_failure`, the rest `user_action`.
- Frozen status vocabularies: `ListingStatus` (9), `ListingResult.status` (3), `MarketplaceState` (8), `JobState` (7).
- `ImageRef` refuses `packaging_label` on a publishable listing. `ImageReport` may still name that slot.
- `GenerationContext.product` stays a dict through v1; the `facts.py` retype moves to after v1.
- Not in this release: `facts.py`, `batches.py`, and `CanonicalListing.attribute_sources`.
