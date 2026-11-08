# Amazon deliberate-error tests (SI11, ADR-0028)

Prove the resolution loop against real Amazon before turning `AMAZON_VALIDATION_PREVIEW_ENABLED` on in
production. Use one connected brand account and **test SKUs only** (prefix `KTEST-`); never a live SKU.

1. **Preview first.** Run each case with `VALIDATION_PREVIEW` (the setting on in staging): confirm Amazon India
   answers `VALID`/`INVALID` and stores nothing (a GET of the SKU is 404). Any other answer opens the account
   breaker (a `marketplace_error_review` task, reason `preview_applied`): stop and record the reply status.
2. **The cases** (feasibility study tests T1-T8): a colour outside the enum (expect `enum_fold`), size `XXL` where
   Amazon wants `2XL` (alias table), a missing derivable attribute (`fill_from_facts` or a seller question), a
   barcode with separators (`identifier_normalize`), a title over the limit (content lane: a fix version to
   approve), brand gating (marketplace lane: no loop), an unknown code (staff lane), a locked attribute
   (`drop_locked`, code 4005033) on a stored SKU.
3. **Read the result** in the listing's issues and submissions, and in the error catalogue view
   (`marketplace_error_catalog`, read as `app_platform_ro`): each code seen, mapped or not, with counts and no
   message text. (The ops page and `app.cli errors catalog` land with SI11 T19.)
4. **Record** every preview code that differs from the production code (F:61) as its own `error_map.amazon.json`
   row in a reviewed PR; then load with `app.cli errors load`.
