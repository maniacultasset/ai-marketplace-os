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
   message text, also on the ops errors page and in `app.cli errors catalog`.
4. **Record** every preview code that differs from the production code (F:61) as its own `error_map.amazon.json`
   row in a reviewed PR; then load with `app.cli errors load`.

## SI13 additions (before `AMAZON_CATALOG_PRECHECK_ENABLED` goes on in production)

Test SKUs only (prefix `KTEST-`), one connected brand account, preview first as above.

- **T9 another brand's barcode.** A test SKU with a real EAN Amazon lists under another brand: expect one
  `|preview` ERROR `IDENTIFIER_DUPLICATE` at `facts.identifier`, marketplace code `catalog:other_brand`, lane
  seller, approve refused; then the same EAN under the brand's own name: a WARNING `catalog:same_brand` only.
- **T10 a restricted brand.** A test SKU whose matched ASIN needs approval: expect an ERROR
  `ACCOUNT_PERMISSION_DENIED` (`restriction:APPROVAL_REQUIRED`) and one `permission_decision` task whose question
  shows Amazon's `https://sellercentral.amazon.in/...` link. Record the restriction call's rate headers.
- **T6/T7 checks.** A test SKU with a product type Amazon refuses (4000003) or a bad browse node (101158): lane
  seller `recategorize` before the first accepted publish, staff after; a child sent without its size attribute:
  one `fill_variant_axis` repair and a PATCH of that child only.
- **Diagnosis.** With `MARKETPLACE_ERROR_DIAGNOSIS_ENABLED` on in staging, an unmapped code drafts one proposal
  within 10 minutes (`app.cli errors proposals list`); review it with `approve --by <admin email>` or `reject`, then
  commit the printed row to `error_map.amazon.json` in a PR.
