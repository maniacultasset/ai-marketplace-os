# ADR-0031: Owners may approve their own listings, per brand, off by default

- **Status:** accepted (owner decision, 11 Nov 2026). Amends invariant 3 (`docs/RULES.md` §1) and
  ADR-0008 decision 1 for one case only.
- **Date:** 2026-11-11
- **Deciders:** the owner (client), relayed in chat on 2026-11-11

## Context

- Invariant 3 is maker-checker: whoever generated or edited a listing version cannot approve it.
  ADR-0008 enforces it in the API (`app/services/maker_checker.py`, `app/services/review.py`) and in
  the database (`lv_guard`, MKC01), and notes that every brand then needs two members (T15, spec
  L5450 "one-person brand").
- Client testers are often a solo owner. The review screen tells them "Someone else must approve"
  and they can never send a listing. The review read's `no_other_approver` hint (SI10) explains the
  block but offers no way out except inviting a second person.
- The owner decided on 11 Nov 2026: add a per-brand setting "Owners may approve their own listings",
  off by default. Only when it is on may a member with role `owner` approve a version they generated
  or edited. Reviewers never approve their own work; operators and admins never approve. Every
  self-approval is visibly recorded.

## Decision

1. **The setting.** `tenants.owner_self_approval boolean NOT NULL DEFAULT false` (migration
   `0030_owner_self_approval`), read on `GET /api/v1/tenants/me` as `owner_self_approval` and
   changed on `PATCH /api/v1/tenants/me`.
   - Only an active `owner` changes it. The route refuses any other role with 403
     `PERMISSION_DENIED` (an admin holds `settings:manage` but not this field), and the service reads
     the membership again `FOR SHARE` inside its transaction.
   - The change needs a step-up like a team change (ADR-0021/0023 "any": a TOTP or recovery code in
     the last 30 minutes under `MFA_ENFORCEMENT=all`; 403 `MFA_STEP_UP_REQUIRED` or
     `MFA_ENROLLMENT_REQUIRED`). The check runs for this field only, so a profile edit never asks
     for a code.
   - Each change writes `tenant.owner_self_approval_changed` with the actor and the value before
     and after (`audit_allowlist('tenant')` keeps `owner_self_approval`).
2. **The rule, exactly.** An approval of a version whose contributor chain contains the approver is
   allowed if and only if the approver's membership is active, its role is `owner`, its email is
   not unverified, and the tenant's `owner_self_approval` is true at the time of the approval. Every
   other check is unchanged: the role check (reviewer or owner), the current version, the passed
   rules run with current pins, the image and marketplace gates, the bulk rule and set-once.
   `maker_checker.approve_block(..., owner_self_approval=)` is the one predicate for the approve
   path and the review read; its default is off.
3. **The backstop.** `lv_guard` (redefined in 0030 from 0026's body) permits the same case and
   nothing more: it reads the tenant's setting itself, lets an active owner in the chain through
   only while it is on, and sets `listing_versions.self_approved` to whether the approver was in
   the chain. Only the trigger writes that column: `app_rw` holds no INSERT or UPDATE on it, an
   INSERT with it true is GRD01, and it is part of the set-once approval. `ck_maker_checker` admits
   an approver equal to the maker only on a row marked `self_approved`.
4. **The record.** A self-approval is visible in three places, none of them rewritable:
   `listing_versions.self_approved` (immutable after the approval), a `listing.self_approved`
   audit row (reason `owner_self_approval`) beside `listing.approved`, and the history read
   (`AuditEventOut.review.self_approval`). `review_decisions` stays as it is (append-only).
5. **Hints.** The review read (`ListingDetail`) gains `self_approval` (true when the caller may
   approve and it would be a self-approval, so the screen can say "You're approving your own work.
   This is recorded.") and the hint `owner_can_enable`: an owner blocked by `own_work` with nobody
   else able to approve while the setting is off. A non-owner in the same position keeps
   `no_other_approver`.
6. **Approve and resend (ADR-0028).** A content fix or AI rewrite makes a new version whose maker
   is the base version's generator; it is approved through the same `POST /listings/approve` path,
   so the same rule applies: under the setting the owner who made the base may approve the fix
   version, recorded as a self-approval. The automatic resend of a non-content repair is unchanged
   (it needs a current human approval and never approves anything).

## Consequences

- A solo owner can approve and publish once it turns the setting on, after a fresh code. Brands
  that leave it off keep two-person approval exactly as before; nothing changes for them.
- The setting weakens maker-checker for owners only, by the owner's own choice, and every use is
  recorded in the version row and the audit log. Reviewers never gain it.
- Turning the setting off takes effect on the next approval: the API and `lv_guard` read it inside
  the approving transaction. An approval that already happened stays recorded as it was.
- Contract 2.1.0: `TenantOut.owner_self_approval`, `TenantPatch.owner_self_approval`,
  `ListingDetail.self_approval` and `AuditReviewOut.self_approval` are additive; the
  `approve_hint` member `owner_can_enable` is a closed-vocabulary addition recorded as a one-time
  out-of-window exception under this owner decision (`packages/schemas/CHANGELOG.md`). The web ships
  the matching screens in the same change.
- A later move to confidence-based auto-publish (ADR-0008 decision 2) is unaffected: this setting
  still needs a human approval of a specific version.
