# PTD refresh (Amazon Product Type Definitions)

DW2 (T53). Staff run these from the migrate job's image (`APP_ROLE=migrate` or `shell`), never
from the api or the worker: those refuse to boot with `AMAZON_PLATFORM_REFRESH_TOKEN` set outside
local. The fetch uses the platform-owned SP-API authorization, or (ADR-0033, `--via-tenant`) the
stored connection of an allow-listed tenant. Output names the product type, version and digest
prefix only.

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

## Through the owner's seller connection (ADR-0033)

While `AMAZON_PLATFORM_REFRESH_TOKEN` is empty, fetch through an allow-listed tenant's stored
Amazon connection instead: add `--via-tenant SLUG` to `templates refresh`. SLUG must be on
`AMAZON_PTD_TENANT_SLUGS` (`prod.env`: `test`, the owner's own brand; blank refuses every tenant),
the staff email must be an active platform admin, and the tenant's Amazon account must be
`active`. Only `getDefinitionsProductType` and the schema download are sent (GET only), the
account row is not changed, and the audit rows name the staff email and `(via tenant SLUG)`. The
platform token is never read, mounted or asked for.

Locally (needs `DATABASE_URL`, `ALEMBIC_DATABASE_URL`, the Amazon app and the token key):
`python -m app.cli templates refresh --product-type SHIRT --staff-email <platform admin> --reason
"<why>" --via-tenant test [--dry-run [--save-schema shirt.json]]`. `--dry-run` stores nothing but
still checks the platform admin, so it needs `ALEMBIC_DATABASE_URL` too.

In production, one job run per product type (job `aimos-ptd-tenant`, identity `aimos-ptd`):

```
scripts/deploy/gcp/deploy.sh prod templates refresh --product-type SHIRT  --staff-email <platform admin> --reason "first load, beta style rejection" --via-tenant test
scripts/deploy/gcp/deploy.sh prod templates refresh --product-type KURTA  --staff-email <platform admin> --reason "first load, beta style rejection" --via-tenant test
scripts/deploy/gcp/deploy.sh prod templates refresh --product-type PANTS  --staff-email <platform admin> --reason "first load, beta style rejection" --via-tenant test
scripts/deploy/gcp/deploy.sh prod templates refresh --product-type SHORTS --staff-email <platform admin> --reason "first load, beta style rejection" --via-tenant test
scripts/deploy/gcp/deploy.sh prod templates refresh --product-type SKIRT  --staff-email <platform admin> --reason "first load, beta style rejection" --via-tenant test
```

Each prints `templates refresh: stored_current <TYPE> version <v> digest <prefix> ... (via tenant
test)` in the job log; a rerun with the same schema prints `unchanged`. The first version of a
type goes in current, so no `promote-definition` is needed on a first load. To confirm in
production, `scripts/deploy/gcp/deploy.sh prod amazon-check --slug test --staff-email <platform
admin>` should show `4 ptd KURTA` and `4 ptd SHIRT` as `in sync with the stored definition`
(read-only). `templates check` and `templates diff` (above) compare every template's believed
names with the stored definitions; run them where `DATABASE_URL` reaches that database.

`deploy.sh` cannot pass the CLI's own `--dry-run` (its `--dry-run` only prints the gcloud
commands), and a `--save-schema` file in a Cloud Run job is lost when the job ends: use those
locally.

| exit | meaning |
|---|---|
| 0 | fetched (dry run), stored, stored_current or unchanged |
| 1 | a missing setting, the token did not open or refresh, an Amazon or database failure |
| 3 | refused: SLUG not on `AMAZON_PTD_TENANT_SLUGS`, not an active platform admin, or no active Amazon account with a stored token |
| 4 | no such tenant |

## DW2-S (sandbox) and DW2-P (production)

- DW2-S: `scripts/smoke/amazon_ptd_sandbox.sh` (manual, never CI) fetches KURTA from the sandbox
  as a dry run, diffs it and scans the output for secrets. Afterwards narrow
  `AMAZON_PTD_LINK_HOSTS` from `.amazonaws.com` to the exact schema host it used.
- DW2-P (trigger: SP-API production app approval): run steps 1-3 in production for KURTA, SHIRT
  and LAMP, then correct the believed names in `app/adapters/amazon/constants.py` and the seed
  templates through a new template version, and confirm the shape of `item_dimensions`.

## Check every category template (client feedback item 5)

`python -m app.cli templates check [--product-type T] [--list-browse-nodes]` (read-only, on
`DATABASE_URL`) checks each Amazon category template against the stored current definition of
its own product type, and prints one line per template:

```
ethnic_wear_kurta_sets -> KURTA: definition <version>, 0 drift(s); browse node not set
home_decor_lamps -> LAMP: no stored definition for LAMP, not checked; browse node not set
tshirts -> SHIRT: no stored definition for SHIRT, not checked; browse node not set
templates check: 3 template(s), 1 checked, 2 without a stored definition, 0 drift(s), 3 without a browse node
```

Exit 0: all checked, no drift. Exit 1: drift (the drift lines are indented under the template).
Exit 3: a template was not checked because no definition is stored (run `templates refresh` for
that product type first), or the check could not run. Without a stored definition a preview or
publish is not checked against Amazon's schema either (AC10, the `ptd.missing` log line).

## Browse nodes

Each Amazon template's browse node is its `external_category_id`. When it is set (digits only),
every item of a listing in that category sends it as `recommended_browse_nodes`
(`[{value, marketplace_id}]`), and `templates diff` / `check` verify it against the stored
definition (declared, and in its enum when the definition lists one). When it is NULL nothing is
sent.

No Amazon.in (`A21TJRUUN4KGV`) node id is in the repo: the trimmed fixtures in
`tests/fixtures/amazon_ptd` are hand-written and carry none, and no real definition has been
stored yet. Every seeded template keeps NULL, and none is guessed:

| category | Amazon product type | browse node |
|---|---|---|
| `ethnic_wear_kurta_sets` | `KURTA` | needed |
| `tshirts` | `SHIRT` | needed |
| `home_decor_lamps` | `LAMP` | needed |

To fill them: store the real definitions (`templates refresh`, then `promote-definition`), run
`templates check --list-browse-nodes` to see the nodes each definition lists (or read them in
Seller Central's browse tree guide for amazon.in), then set `external_category_id` in
`app/db/seed_templates.py` as a new template version, regenerate
`app/db/seed_data/platform_templates.json`, and load it (`templates load`). `templates check`
should then print `browse node set` with 0 drift.
