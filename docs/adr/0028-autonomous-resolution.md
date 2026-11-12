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
- Open: SP-API notifications (EventBridge) instead of polling; preview support on Amazon India unconfirmed (F:61).

## SI13 additions (proposed, 2026-11-08)

- **Category reassignment.** A status read keeps Amazon's `summaries[0].productType`; a different type, or a burst
  of 3+ MISSING_ATTRIBUTE errors on an accepted SKU, is one `CATEGORY_MISMATCH` item at `facts.category` that
  holds the burst's questions (one ask, not ten). The seller recategorises (`recategorize` step) when the listing
  was never live, else staff reviews (CP-D7). A rejected, never-live listing with the open item may be
  recategorised; it keeps `failed` and the seller's regenerate voids the approval (no new edge, CP-D19: the user
  `edit` edge needs an edit version).
- **Catalog and restriction pre-check** inside the dry run (CP-D6, both switches): another brand's barcode is an
  ERROR the seller fixes (`edit_identifier`); a restricted brand is a `permission_decision` task with Amazon's
  Seller Central link (https, `sellercentral.amazon.in|com` only, CP-D13). A failed check never blocks.
- **Seller actions with no inbox question** (CP-D8): `recategorize` and `edit_identifier` stay seller steps; the
  issue row (lane seller, path, message) is the signal and `NextStepOut.kind` names the action.
- **Child theme axis** (`fill_variant_axis`): a child sent without its theme attribute gets its own variant value.
- **AI diagnosis of unknown codes** (CP-D1, CP-D2): a `brand_embed` job per unmapped code drafts one proposal
  over the pipeline's `complete_json`, through the SI10 budget, with every AgentRun persisted. **Proposals never
  act**: only a platform admin's `errors proposals approve` (full re-check of the final row) writes a map row,
  whose `policy_ref` names the proposal and the approver's user id (CP-D20). The `--by` email is resolved by the
  migrator connection; migrator access is the authorisation and the seed PR the second review.
- **Value maps and the export** are read-only staff reports (`errors value-maps`, `errors export`); an alias change
  stays a reviewed PR to `aliases.py`.

## Client feedback item 20 additions (2026-11-11)

- **Coverage.** `error_map.amazon.json` adds the codes on Amazon's Listings Items troubleshooting and Manage
  Listings Issues pages (missing values, enums, patterns and fabric, lengths, counts, numbers, cross-field rules,
  identifiers and the catalogue, luxury and generic-brand eligibility, price limits, currency, Amazon's internal
  error), each with plain text and a seller action; `policy_ref.kuinn_repair` names the safe repair kinds that can
  cover a row. No row sets its own fix (`auto_fixable` false, no `fix_action`), but the planner routes on the
  code, so a row's code is its lane and is chosen on purpose (`test_error_map_coverage`): a fact value (fabric
  make-up, price, a count, a number) is `VALUE_NOT_ALLOWED`, `CROSS_FIELD_MISMATCH` or `MISSING_ATTRIBUTE` and goes
  to the seller or Kuinn's team, never to the AI; only a text rule on Kuinn's own text (HTML, web links, a length)
  takes the deterministic content fix (`FIELD_TOO_LONG`) or the one-field AI rewrite (`PATTERN_MISMATCH`,
  `FIELD_TOO_SHORT`, credit-gated, one try per key); Amazon's internal error 4000000 (`MARKETPLACE_UNAVAILABLE`;
  the row stays `user_action`, as every documented row) is resent by its code's policy; brand and generic-product eligibility goes to the marketplace lane; the luxury-category
  codes 4005018/4005019 are `CATEGORY_MISMATCH` (the seller recategorises). `error_map.flipkart.json` is new: Flipkart documents the error shape but no code list, so its rows match
  the documented payload node an issue's path names (`path:price`, `path:tax`, ...); settings Kuinn does not send
  in v1 (tax, delivery charges, stock, packages, fulfilment) carry `policy_ref.handled_outside`. The migrate job
  loads both files.
- **Issue history read.** `GET /listings/{id}/issue-history`: each marketplace ERROR with its plain cause, the
  marketplace's code and words, the field, the client's error group and who handles it (Kuinn, the seller, the
  marketplace side, or Kuinn's team; a resend is Kuinn's), and each fix with its before and after values (a content
  fix's or AI rewrite's after value is read from the fix version that closed the issue), reason and outcome. It reuses
  this read's lanes, never runs the engine and never writes.

## Amendment: the one-off dry run (owner decision, 2026-11-12)

- **Decision.** The owner asked for Amazon's `VALIDATION_PREVIEW` (which saves nothing) on 2 or 3 test listings
  in the brand that holds the real seller connection, through a one-off staff job, while publishing stays off
  everywhere else (`AMAZON_PUBLISH_ENABLED=false` in `prod.env` is unchanged).
- **How.** `deploy.sh prod amazon-preview --slug S --listing-id L --staff-email E [--precheck]` creates the
  Cloud Run job `aimos-amazon-preview` (worker identity, shell role, `DATABASE_URL` and `AMAZON_CLIENT_SECRET`
  only), executes it once, prints its log and deletes it. Only that job's env sets `AMAZON_PUBLISH_ENABLED=true`,
  `AMAZON_VALIDATION_PREVIEW_ENABLED=true` and `AMAZON_PREVIEW_ONEOFF=1`; every role but the shell refuses to
  boot with the last one, and `app.workers.amazon_preview` refuses to run without it.
- **What it may send.** The job body of point 4 (`run_remote_preview`) runs once, synchronously, behind a
  transport that passes a GET or a listings-item PUT whose only `mode` is `VALIDATION_PREVIEW` and refuses
  anything else before it is sent, and an adapter wrapper with no create, update or publish method. A GET of each
  SKU runs before (a SKU already on Amazon stops the run before any preview) and after (a SKU found, or a reply
  other than VALID or INVALID, prints STOP and fails the job; any STOP after the preview also opens the CP-D24
  breaker task). Every seller SKU must carry the test prefix `KTEST-`: the job checks the listing's SKUs before
  any call and the built item's SKUs again right before the preview, and refuses a live SKU unsent.
- **What it never does.** No job of any kind is enqueued (`no_enqueue` around the whole run: no publish, no
  automatic resend, no fix job); the kind's terminal hook and the resolution engine never run; the verdict is
  printed, not written (no issue rows, no `remote_check_*`, no status move), so a test listing is never left
  blocked by a dry run production cannot repeat. One `listing.preview_oneoff` audit row names the staff admin.
- Runbook: `docs/runbooks/AMAZON-PREVIEW-ONEOFF.md`.
