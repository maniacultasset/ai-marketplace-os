# ADR-0015: Listing lifecycle and the publish ledger

- **Status:** proposed
- **Date:** 2026-10-06
- **Deciders:** Tech Lead

## Context

DATABASE.md open decision 22 asks for an ADR on the listing lifecycle and the publish ledger before
`0004_integrity`. The design is in DATABASE.md §6 (the 9 statuses, the 30-event vocabulary, the 63-edge
transition table, the display projection) and §5.3 (`listing_submissions`, `publish_field_results`, the
write-ahead protocol and the anti-loop rules).

## Decision

`listings.status` changes only along `listing_status_transitions`, a platform table seeded by
migration and read by the table-driven `listings_status_guard`; `last_event` is CHECKed against the 30
events. Each transition's actor (user or worker) needs its GUC, and its approval and reason flags hold.
`approved` and `publishing` need the current version human-approved and the tenant trial or active; a
paused or archived listing changes no status. The event rules need their evidence in the same
transaction: an edit version, a review decision decided now by the acting user, a queued generate or
publish job (so a publish writes INSERT `job_runs` before UPDATE `listings`), fewer than three
submissions, an accepted submission. The publish gate on `listing_publish` jobs and `ls_guard` on
`listing_submissions` make the ledger the backstop for invariants 1, 3, 9 and 10: the sent version is
the current approved one, its facts digest equals the approval's, at most three attempts per version,
and a retry is justified (`changed_input`, `retryable_condition`, `marketplace_transition`,
`human_resolution`). The display (the 14 BRD names) is computed by IMMUTABLE functions and two
`security_invoker` views, never stored.

## Consequences

- Every status write must set `last_event` explicitly and its GUCs; a forgotten prerequisite is a 409
  (`INVALID_STATUS_TRANSITION`), not a silent state.
- Adding an edge or an event is a migration (a new transition row and a widened CHECK).
- The issues and human-task lifecycle is a separate decision (open decision 22's second half).
