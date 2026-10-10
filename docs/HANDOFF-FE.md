# Handoff to the FE team: the v1 API

The backend serves one versioned REST API under **`/api/v1`**. Its contract is `openapi.json` in `packages/schemas`, released as **`schemas-v1.0.0`**.

## 1. Install the contract

```json
"@mkos/schemas": "npm:@maniacultasset/schemas@1.0.0"
```

- **The package.** It is on GitHub Packages, in the `maniacultasset` org. Your `.npmrc` needs a token with `read:packages`.
- **What's in it:** the generated TypeScript types for every request and response, and the OpenAPI document.
- **Never hand-write a type the package already has.**
- **Version rule.** Later additive changes ship as `1.x` MINORs, and the CHANGELOG lists each one. Breaking changes wait for the MAJOR window.

## 2. Work before the backend is deployed

- **The mock server.** `make mock` serves every operation from the OpenAPI examples on `127.0.0.1:4010`. Send `Prefer: code=<status>` to get a declared error. `make mock-smoke` checks that it answers.
- **The real API on your machine.** Run `make dev`, then `make seed`, in the backend repo; it serves on `127.0.0.1:8000`. The demo seed creates two tenants (`acme` and `bharat`), their users and 8 products.

## 3. Conventions every screen relies on

| Topic | Rule |
|---|---|
| Auth | `POST /auth/login` returns an access token and a refresh token. Send `Authorization: Bearer <access>`, and renew with `POST /auth/refresh`. `GET /auth/me` returns your memberships, and `POST /auth/switch-tenant` changes tenant. The tenant comes only from the token: no route takes a `tenant_id`. |
| Roles | `owner`, `admin`, `operator` and `reviewer`. A forbidden action is 403, and another tenant's object is 404, never 403. |
| Errors | Every error has one envelope: `{"error": {"type", "message", "status", "correlation_id", "details", "error_report"}}`. `error_report` is the shared `ErrorReport`. Show `message`, and log `correlation_id`. |
| Idempotency | These operations take an `Idempotency-Key` header: `products_bulk`, `imports_rows_retry`, `brand_documents_upload`, `images_upload`, `listings_generate`, `listings_regenerate`, `listings_approve`, `listings_publish`, `listings_retry_fields` and `jobs_cancel`. A retry with the same key and body replays the first answer. The same key with a different body is 409 `IDEMPOTENCY_KEY_REUSED`. |
| Async work | Long work returns **202** with a `job_id`. Poll `GET /jobs/{job_id}` until it reaches a terminal status, and use `POST /jobs/{job_id}/cancel` to stop it. |
| Pagination | Every list answers `{"data": [...], "page": {"next_cursor", "has_more", "limit"}}`. Pass `limit` (default 50, max 200) and `cursor`. There is no offset or page number. |
| Concurrency | `PATCH /tenants/me` uses `ETag` / `If-Match`. A stale write is 409, so re-read and retry. |
| Rate limits | Per tenant and per IP. A 429 carries `Retry-After`. |
| Request ids | Send `X-Request-ID` if you have one, and the API echoes it. Otherwise it generates one. |
| CORS | Only the `ALLOWED_ORIGINS` origins get a grant, with no credentials. Every method the API serves (`GET`, `POST`, `PATCH`, `DELETE`) is preflight-allowed. `ETag`, `Retry-After`, `Location` and `X-Request-ID` are exposed, so cross-origin JS can read them. |

## 4. The v1 screens and their operations

| Screen | Operations |
|---|---|
| Sign in, team | `auth_*`, `members_list`, `members_patch` |
| Brand profile and documents | `tenants_me`, `tenants_me_update`, `brand_documents_upload`, `brand_documents_list`, `brand_documents_delete` |
| Products and CSV import | `products_create`, `products_get`, `products_list`, `products_bulk`, and the import status, report and row-retry ops |
| Images | `images_upload_url` (signed PUT), `images_upload` (multipart), `images_report` |
| Marketplace accounts | `marketplaces_connect`: redirect the browser to the returned consent URL. Then `marketplaces_connect_confirm` after the callback lands, `marketplaces_accounts` and `marketplaces_accounts_revoke` |
| Listings: generate, review, edit, approve | `listings_generate`, `listings_regenerate`, `listings_get`, `listings_list`, `listings_content_patch`, `listings_validate`, `listings_approve`, `listings_status` |
| Publish | `listings_publish`, `listings_retry_fields` |
| Analytics | `analytics_get`, `analytics_summary` |
| Rules (support) | `rules_list`, `rules_dry_run` |

Until a backend step ships, its operations answer **501** on the real API, but they are fully specified in the contract and the mock.

## 5. The marketplace connect flow, step by step

1. The owner clicks "Connect Amazon" or "Connect Flipkart". Call `marketplaces_connect`, then send the browser to the consent URL it returns.
2. The marketplace sends the browser back to the backend's callback. The backend validates the state, exchanges the code, seals the tokens, and redirects to the dashboard with a one-time handle.
3. The dashboard calls `marketplaces_connect_confirm` with that handle. The account appears in `marketplaces_accounts`. Its status is `active`, or later `needs_reconnect`, which means you should show a "Reconnect" button.
