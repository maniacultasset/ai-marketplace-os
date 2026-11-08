# ADR-0028: Autonomous listing resolution and the automatic resend

Status: proposed (SI11; number held since SI4; the user's resend rule given in chat 2026-11-08 under the V1
delegation, relayed by the conductor)

## Context

v1 must fix marketplace errors on its own where that is safe, and stop cleanly where it is not (PRD story 6; spec
L1061-1067 "Auto-Fixable Classes", L1336-1358 "Error Resolution Loop" and its anti-loop rule; T41; VISION Q4).
Invariant 1 says only adapters publish, and only after human approval. A marketplace rejection of an approved
version raises a question the invariants did not answer: may a deterministic, adapter-side change of that version's
payload go back to the marketplace without a second approval?

## Decision

1. **The user's resend rule (rank 0).** After a marketplace rejection, only non-content adapter repairs on a current
   approval resend automatically, at most 3 automatic cycles. Content fixes and AI rewrites make a new version that
   a non-maker approves ("Approve and resend"). This amends invariant 1 for non-content adapter repairs only: the
   approved content is unchanged, the canonical listing is unchanged, and the resend is audited as `system`.
2. **Repairs are adapter data** (`listing_repairs`): one marketplace attribute's value (`enum_fold`, `unit_format`,
   `identifier_normalize`, `exemption_flag`, `fill_from_facts`, `drop_locked`, `image_slot`), applied only while
   the seller's source value still hashes to the repair's digest. Text, keywords, price, MRP and the manufacturer
   are never repaired. A seller edit makes a repair stale (retired `source_changed`).
3. **What resends without a human:** only when every blocking issue is an `auto_repair` (or plain `retry`), the
   approval is current (`requires_current_approval`), the version has fewer than 3 submissions, and fewer than 3
   automatic jobs ran since the last human event. The resend is `failed -> publishing` on `auto_resubmit`, actor
   `system`, `changed_input`, `corrections_json` from the adapter's repairs, the previous requester kept (SUB01).
   It runs now (CP-D22). A `published` listing (BUYABLE with ERROR issues) never resends (CP-D21): its repairs ride
   the next submission of any version.
4. **The marketplace's dry run before approval** (Amazon `VALIDATION_PREVIEW`, which saves nothing) is a second
   body of the `listing_status_sync` kind (no new `JobKind`). It sends no images (CP-D23: no public copy of an
   unapproved image), writes its issues in their own `preview` channel, and refuses approval while an ERROR of the
   current version is open. A reply that looks applied opens an account breaker (CP-D24). Off by default
   (`AMAZON_VALIDATION_PREVIEW_ENABLED`); production turns it on after the deliberate-error runbook passes on India.
5. **The engine runs in the terminal hook** of the job that produced the answer, after that job's own writes, in a
   SAVEPOINT (CP-D2): a bug never loses the job's result. The cycle count is never stored (CP-D7): jobs carry
   `trigger: "resolution"`.

## Consequences

- Invariant 1 gains one documented exception (`docs/RULES.md` §1 note, explicitly approved by the user 2026-11-08).
- Invariant 9 is enforced twice: the 3-cycle cap here and `ls_guard`'s 3 submissions per version.
- The error database grows from real issues: the `marketplace_error_catalog` view (no tenant id, no message text)
  feeds a reviewed PR to `error_map.amazon.json`.
- Open: SP-API notifications (EventBridge) instead of polling; preview support on Amazon India unconfirmed (F:61);
  catalog and restriction pre-checks; AI diagnosis of unknown codes.
