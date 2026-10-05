"""API-local request and response models for `/api/v1` (S2 Decision 7: product and other API
shapes live in `apps/api` and reach the FE through `openapi.json`). Requests are `StrictModel`
(unknown keys rejected); responses are `WireModel`. No request model carries `tenant_id`: the
tenant comes only from the JWT `tid` claim (docs/RULES.md §3)."""
