# Amazon one-off dry run (VALIDATION_PREVIEW) on test listings

## The owner decision (2026-11-12)

Run Amazon's `VALIDATION_PREVIEW` on 2 or 3 test listings in the brand that holds the real seller connection, through a
one-off staff job. `VALIDATION_PREVIEW` asks Amazon to check a listing and saves nothing. Publishing stays off everywhere
else: `AMAZON_PUBLISH_ENABLED=false` stays in `prod.env`, and the api and the worker never get the dry-run switches.
ADR-0028 (amendment of 2026-11-12) records the design.

## What the job does and does not do

- `deploy.sh prod amazon-preview ...` creates the Cloud Run job `aimos-amazon-preview` (worker identity, shell role,
  `DATABASE_URL` and `AMAZON_CLIENT_SECRET` only), executes it once, waits, prints its log, then deletes the job.
- Only that job's env has `AMAZON_PUBLISH_ENABLED=true`, `AMAZON_VALIDATION_PREVIEW_ENABLED=true`,
  `AMAZON_CATALOG_PRECHECK_ENABLED` (true only with `--precheck`) and `AMAZON_PREVIEW_ONEOFF=1`. Any role but the shell
  refuses to boot with `AMAZON_PREVIEW_ONEOFF`, and the job refuses to run without it.
- It refuses (nothing sent) unless: the staff email is an active platform admin; the listing is Amazon, not paused or
  archived, in `validated`, `needs_review`, `approved` or `blocked/validation_errors`; its account is `active`; the
  account's preview breaker is closed; no `listing_status_sync` job of the listing is live; and every seller SKU
  it would send starts with `KTEST-` (checked on the listing's SKUs before any call, and again on the built item
  right before the preview).
- It GETs each SKU first. If any SKU already exists on Amazon it prints STOP and sends no preview.
- It runs the preview job body once. Every SP-API request must be a GET or a listings-item PUT with
  `mode=VALIDATION_PREVIEW`; anything else is refused before it is sent. `--precheck` adds the read-only catalog search
  and brand-restriction read.
- It GETs each SKU again and prints the verdict, each issue (code, Amazon's code, severity, attribute path, message),
  and each SKU's preview reply and GET result.
- It never enqueues a job (no publish, no automatic resend, no fix), never runs the resolution engine, and writes no
  verdict to the listing (no issue rows, no status move). It writes one `listing.preview_oneoff` audit row. On any
  STOP after the preview (a SKU found on Amazon, or a reply that is not VALID or INVALID) it also opens the account's
  breaker task (`marketplace_error_review`, `account.preview_applied`).

Exit codes: 0 done (every SKU still 404), 1 failure, 3 refused by settings, 4 not eligible, 5 STOP. Any non-zero exit
fails the deploy command, after the job is deleted.

## Steps

1. **Pick the test listings (read-only).** Through `cloud-sql-proxy` as `app_ro` (read-only role, see
   `INCIDENTS.md`), list the brand's Amazon listings that can be previewed, with their SKUs. Use test SKUs only
   (prefix `KTEST-`); never a live SKU. The job also refuses (exit 4, nothing sent) a listing with any SKU
   without that prefix, a variant's included.

   ```sql
   SELECT l.id AS listing_id, l.status, l.status_reason, p.sku AS seller_sku,
          l.remote_check_status, a.status AS account_status
     FROM listings l
     JOIN tenants t ON t.id = l.tenant_id
     -- the SKUs the dry run sends: the product and its variants (listing_items is empty until
     -- a first submission)
     JOIN products p ON p.tenant_id = l.tenant_id AND p.deleted_at IS NULL
                    AND (p.id = l.product_id OR p.parent_product_id = l.product_id)
     LEFT JOIN marketplace_accounts a
       ON a.tenant_id = l.tenant_id AND a.marketplace = 'amazon' AND a.status <> 'revoked'
    WHERE t.slug = '<slug>'
      AND l.marketplace = 'amazon'
      AND l.paused_at IS NULL AND l.archived_at IS NULL
      AND (l.status IN ('validated', 'needs_review', 'approved')
           OR (l.status = 'blocked' AND l.status_reason = 'validation_errors'))
    ORDER BY l.created_at DESC
    LIMIT 20;
   ```

2. **Dry run of the command** (prints every gcloud call and the job's env file, runs none):

   ```bash
   scripts/deploy/gcp/deploy.sh prod amazon-preview --slug <slug> --listing-id <listing_id> \
     --staff-email <platform admin email> --dry-run
   ```

   Check the env file: `AMAZON_PUBLISH_ENABLED: "true"` once, `AMAZON_PREVIEW_ONEOFF: "1"`, `APP_ROLE: "shell"`.

3. **Run it for one listing**, without `--dry-run`. Add `--precheck` only if the owner wants the catalog and
   restriction check too.
4. **Read the log.** Expect `verdict: clean` or `verdict: issues`, the issue lines, and for each SKU
   `before 404, preview reply VALID|INVALID, after 404`. Record each issue code in the client notes.
5. **Repeat** for the second and third listing. Stop at three.
6. **Confirm nothing stayed behind:**
   `gcloud run jobs list --region=asia-south1 --project=<project>` shows no `aimos-amazon-preview`, and the api and
   worker env still has `AMAZON_PUBLISH_ENABLED=false` (`deploy.sh prod status`).

## If it prints STOP

- **A SKU exists on Amazon (before the preview):** nothing was sent. Pick another test listing.
- **A SKU exists after the preview, or a reply was not VALID or INVALID:** Amazon may have stored the item. Do not run
  the job again. Check the SKU in Seller Central and tell the owner. The account's preview breaker task is open, and it
  blocks any later dry run on the account until staff close it.
- **A request was refused by the transport:** nothing past the refusal was sent. This is a bug: open an incident with
  the job log.

## Rollback

Nothing to roll back in the app: no setting of the api or the worker changed, and no job or verdict was written. If
the deploy command was interrupted after the job was created, delete it by hand:

```bash
gcloud run jobs delete aimos-amazon-preview --region=asia-south1 --project=<project> --quiet
```

To remove the capability itself, revert the PR that added `amazon-preview`.
