# PTD refresh (Amazon Product Type Definitions)

DW2 (T53). Staff run these from the migrate job's image (`APP_ROLE=migrate` or `shell`), never
from the api or the worker: those refuse to boot with `AMAZON_PLATFORM_REFRESH_TOKEN` set outside
local. The fetch uses the platform-owned SP-API authorization, never a tenant's token. Output names
the product type, version and digest prefix only.

## Refresh, check, promote

1. Fetch and store: `python -m app.cli templates refresh --product-type KURTA --staff-email
   <you> --reason "<why>"`. Needs `AMAZON_CLIENT_ID`, `AMAZON_CLIENT_SECRET`,
   `AMAZON_PLATFORM_REFRESH_TOKEN` and `ALEMBIC_DATABASE_URL`. The first version of a product
   type goes in current (`stored_current`); the same schema again is `unchanged`; a changed one is
   `stored`, not current.
2. Check the believed wire names: `python -m app.cli templates diff --product-type KURTA` (the
   stored current definition, read-only on `DATABASE_URL`). Exit 0 no drift, 1 drift (one line
   each), 2 missing `--from-file`, 3 could not compare.
3. Promote a stored version after review: `python -m app.cli templates promote-definition
   --product-type KURTA --version <v> [--digest <prefix from step 1>] --staff-email <you> --reason
   "<why>"`. Exit 4 when no single row matches (add `--digest`).

From then on a preview and a first publish of an Amazon KURTA listing are checked against the
current definition; an update publish is not blocked by it.

`--dry-run [--save-schema PATH]` fetches without a database and saves the verified schema to a new
0600 file, for `templates diff --from-file PATH`.

## In production (deploy toolkit)

1. Once: authorise the Kuinn app on the company's own seller account (Solution Provider Portal ->
   the app -> Authorize, a self-authorization) and add the refresh token it shows as
   `AMAZON_PLATFORM_REFRESH_TOKEN` with the `read -s` command `templates` prints. Never paste it
   in a chat or a file.
2. `scripts/deploy/gcp/deploy.sh prod templates refresh --product-type KURTA --staff-email <you>
   --reason "<why>"`, then `templates promote-definition ...` as in step 3 above. The job
   `aimos-ptd` is the only place the platform token is mounted.

## DW2-S (sandbox) and DW2-P (production)

- DW2-S: `scripts/smoke/amazon_ptd_sandbox.sh` (manual, never CI) fetches KURTA from the sandbox
  as a dry run, diffs it and scans the output for secrets. Afterwards narrow
  `AMAZON_PTD_LINK_HOSTS` from `.amazonaws.com` to the exact schema host it used.
- DW2-P (trigger: SP-API production app approval): run steps 1-3 in production for KURTA, SHIRT
  and LAMP, then correct the believed names in `app/adapters/amazon/constants.py` and the seed
  templates through a new template version, and confirm the shape of `item_dimensions`.
