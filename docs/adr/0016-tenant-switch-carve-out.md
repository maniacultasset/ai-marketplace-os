# ADR-0016: The tenant switch is the one request field naming a tenant

- **Status:** proposed
- **Date:** 2026-10-06
- **Deciders:** Tech Lead (S10 plan approval, PLAN Decision 4); the team confirms

## Context

`tenant_id` reaches the API only from the JWT `tid` claim (docs/RULES.md §3; ADR-0006 §3), so a
request can never name the tenant it acts in. A user with memberships in several brands still needs
to move between them, and ADR-0006 says "switching brands issues a new token" without naming the
endpoint (BACKEND-ARCHITECTURE.md §5.2: "Path: not specified"; DATABASE open decision OQ12).

## Decision

`POST /api/v1/auth/switch-tenant {target_tenant_id}` is the one carve-out. The field is named
`target_tenant_id`, not `tenant_id` (CODE-PLAN S10 Q9), so the contract test that no request field
is called `tenant_id` stays strict and the exception stays visible in the name. The caller must hold
`tenant:read` in its current token's tenant; the target is checked only against the caller's own
active memberships in live (`trial`/`active`) tenants (the `memberships_own` and `tenants_switcher`
RLS branches, DATABASE.md:230, :258, :310). Anything else is 404 `NOT_FOUND`. A switch issues a new
access and refresh pair for the target in a new refresh family, moves the target membership's
`last_used_at`, and writes `auth.tenant_switched` under the target tenant. Tokens already issued
keep their `tid` until they expire.

## Consequences

- One reviewed place reads a tenant from a request body; every other route keeps taking it from
  `tid` only. `test_no_tenant_id_in_requests` and the switch's own 404 tests guard both sides.
- The FE switches brands by calling the endpoint and replacing both tokens; the next login lands on
  the most recently used tenant.
- A stolen access token still works in its own tenant until it expires (CANON.md:82 accepts this
  for logout too); the switch does not revoke the old family.
