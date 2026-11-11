# ADR-0032: An owner adds another brand without a new sign-up review

- **Status:** ⚑ proposed (client feedback item 18, 11 Nov 2026; owner chose the light path in chat)
- **Date:** 2026-11-11
- **Deciders:** Tech Lead (the user)
- **Amends:** ADR-0019 ("tenant creation still needs a platform admin") for a caller who already
  owns an approved, live brand. A new seller still signs up and waits for review.

## Context

One tenant is one brand (ADR-0006, Canon D2). Users are global, `tenant_memberships` gives one user
several brands, and `POST /auth/switch-tenant` moves between them (ADR-0016). Agencies run several
client brands. Today each extra brand needs a new sign-up request and a platform admin's approval,
even when the person asking is an owner the admin already vetted. The client asked for a simple way
to add brands. A real workspace above brands touches about 36 tables, so the owner chose the light
path: keep one brand per tenant and let an approved owner add brands directly.

## Decision

1. **`POST /api/v1/tenants`** (`tenants_create`, 201 `TenantCreated`) takes the brand `name` and an
   optional `website`, the same rules as the sign-up form. The caller becomes the new brand's only
   owner and switches to it with the existing `POST /auth/switch-tenant`.
2. **Who may.** The caller must be an `owner` of the brand the token names. The membership probe
   has already proved that brand live (`trial` or `active`) and the membership and user active,
   and a platform admin (or staff) approved it when it was made, so the review has already happened
   for this person. The action is `settings:manage` (owner, admin); the service then refuses an
   admin (403 `PERMISSION_DENIED`). An owner who also owns a `suspended` brand is refused, so a
   suspended trial cannot be escaped by adding a fresh one; a suspended client brand they only
   operate or review in does not refuse them. A pending or rejected
   applicant has no membership and so no token (401).
3. **Why no review is needed.** The review in ADR-0019 vets a person and an email nobody has proven.
   This caller is that vetted person, signed in, with a fresh code (decision 5). The new brand gives
   them nothing beyond what they already hold: an empty tenant isolated by RLS like any other, no
   marketplace account, no other member, and no extra trial time (decision 4). Platform
   admins still see every tenant in `/admin/tenants` and can suspend or activate it there.
4. **Made like an approved sign-up.** The same slug rule (`slug_candidates`), `trial`, and the
   staff CLI's own statements through
   `SignupRepository.create_tenant`, so a CLI tenant, an approved sign-up and an added brand are
   made by one code path. **The trial clock is not reset.** While no brand the caller owns is
   `active` (paid, or activated by a platform admin), the new brand's `trial_ends_at` is the latest
   one among their live owned trial brands, so it is suspended with them and adding brands never
   buys more free time. An owner of an `active` brand gets a fresh `DEFAULT_TRIAL_DAYS` (ADR-0029,
   30). A website goes on the new brand profile (`website_url`). The audit rows
   are `tenant.created`, `membership.created` and `tenant.added_by_owner`, as `user` rows naming
   the caller (`staff_ops` gains an optional user actor; the CLI and the approval keep `staff`).
5. **Guards.** Step-up `any` (a code in the last 30 minutes under `MFA_ENFORCEMENT=all`); a required
   `Idempotency-Key` (a replay answers the stored brand and makes nothing); a per-user limiter
   (`tenant-create:<user_id>`, the login's per-minute rate) on top of the tenant limiter; a cap on
   the live brands one user owns, `TENANT_CREATE_MAX_OWNED` (default 10, 1-100), held under a
   per-user advisory lock so parallel calls cannot pass it. **AI cost.** The AI budget is per
   tenant (`LLM_TENANT_DAILY_BUDGET_USD`, 2.5) under a platform cap (`LLM_DAILY_BUDGET_USD`, 12),
   so one user's spend is bounded by owned brands x the tenant budget. An owner with no `active`
   brand owns at most `TRIAL_MAX_OWNED` = 3 (7.5 USD a day, under the platform cap), so an unpaid
   trial cannot use up the day's budget for every seller. A paying agency may own up to the cap
   (10 x 2.5 = 25 USD a day at most); raise `LLM_DAILY_BUDGET_USD` with their number of brands, or
   lower the cap, before that matters. The refusals share 403
   `PERMISSION_DENIED`, the message saying which rule refused: no new error code (a MAJOR change).
6. **Switch.** `TENANT_CREATE_ENABLED`, off by default; off, the route is 501 and
   `capabilities.tenant_create_enabled` is false so the dashboard hides "Add another brand".
7. **Out of scope.** A view across brands: RLS is per tenant, so the brand switcher stays the brand
   filter. A workspace above brands, billing per agency and moving a brand between owners wait for
   the full model.

## Consequences

- An agency adds client brands from the brand switcher in one short step and moves between them as
  today; each brand's data stays isolated by the database.
- An owner on trial can add up to two brands that end with their own trial; only an owner of an
  `active` brand starts fresh trials, up to the cap. The caps, the suspended-brand refusal and the
  audit rows bound and record it; an admin can suspend any of them.
- No migration and no shared-schema change: the contract change is one additive op and one
  optional capability field (MINOR, under the open 2.1.0 heading).
