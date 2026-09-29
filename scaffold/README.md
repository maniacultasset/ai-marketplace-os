# Scaffold

Files that the architecture referenced but never wrote, at their intended repo paths.
Copy into the real repo once it exists:

```bash
cp -R scaffold/. /path/to/ai-marketplace-os/
```

| Path | Purpose |
|---|---|
| `apps/api/Dockerfile` | One multi-stage image serving api, worker, migrate and seed; APP_ROLE parameterises the entrypoint so both Cloud Run services run the same digest. |
| `apps/api/docker-entrypoint.sh` | The parameterised entrypoint: APP_ROLE selects api, worker, migrate or seed out of the one image. |
| `.dockerignore` | Repo-root ignore file (the build context is the root); keeps secrets, venvs and node_modules out of the image and the layer cache warm. |
| `apps/api/app/workers/main.py` | Worker process: ARQ consumer plus a $PORT health server in one process, so the worker can deploy as a Cloud Run service. |
| `apps/api/app/core/config.py` | The canonical env register as a pydantic-settings Settings class; four required vars, everything else defaulted, refuses to boot otherwise. |
| `apps/api/app/db/session.py` | Async engine factory, lazy sessionmaker, the FastAPI dependencies and the unit-of-work / platform-scope transaction boundaries. |
| `packages/schemas/src/mkos_schemas/rules.py` | The rules-table row format as a discriminated union, plus RuleSet - the object RulesEngine.validate() takes and load_rules() returns. |
| `packages/schemas/fixtures/rules.seed.json` | Four real rules-table rows - the two title caps, the banned-term list and a required attribute - loaded by scripts/seed.py and by the rules fixture suite. |
| `packages/pipeline/src/mkos_pipeline/state.py` | AgentState: the LangGraph channel schema plus the two adapters that make GenerationContext -> GenerationResult the only cross-team seam. |
| `apps/web/eslint.config.mjs` | ESLint 9 flat config for apps/web - the web-lint branch-protection gate, with the eslint / tsc / prettier boundary made explicit. |
| `apps/api/app/services/imports/row_model.py` | The bulk-import CSV contract as executable code: header validation (file-level 422) and the per-row Pydantic model (row-level errors). |
| `scripts/seed.py` | One command that creates two tenants with deliberately identical SKUs, so cross-tenant isolation is testable; deterministic, re-runnable, no GCP or LLM credentials. |

## Bulk import CSV contract

| Column | Required | Validation rule |
|---|---|---|
| `sku` | required | Trimmed, case preserved. Matches ^[A-Za-z0-9][A-Za-z0-9._/\-]{0,63}$ (1-64 chars). Must be unique within the file - a second occurrence is a row error DUPLICATE_SKU_IN_FILE. A SKU that already exists for this tenant UPDATES that product (UNIQUE (tenant_id, sku)); it never inserts a second row, which is what makes re-uploading the same CSV safe. |
| `title` | required | 1-500 characters after trimming; newlines and repeated whitespace collapsed to single spaces. Empty or whitespace-only is a row error. Marketplace caps (Amazon 200, Flipkart 150) are rules-engine concerns at listing time, NOT import time - a 300-char source title imports fine and fails validation later. |
| `category` | required | Internal slug, ^[a-z0-9_]{2,64}$ (never an Amazon browse node or a Flipkart category id). Must exist in category_templates for at least one marketplace, else row error CATEGORY_NOT_MAPPED. |
| `description` | optional | Free text, <= 5000 characters after trimming. Blank -> empty string. Embedded commas and newlines must be RFC-4180 quoted. |
| `brand` | optional | Free text, <= 120 characters. Blank -> defaults to the tenant name. This is a sub-brand label only; it never overrides tenant scoping. |
| `mrp_inr` | optional | Decimal >= 0 with at most 2 decimal places. Leading currency symbol, spaces and thousands commas are stripped ('₹1,299.00' -> 1299.00). Blank -> NULL. Anything else is a row error. |
| `price_inr` | optional | Same parsing as mrp_inr. If both are present, price_inr must be <= mrp_inr, else row error. Blank -> NULL. |
| `gtin` | optional | Digits only, length exactly 8, 12, 13 or 14 (EAN-8 / UPC-A / EAN-13 / GTIN-14). Leading zeros preserved - the parser reads every cell as text, never as a number. Check digit is not verified in the MVP. Blank -> NULL. |
| `attributes` | optional | Pipe-separated key=value pairs: 'fabric=cotton\|colour=navy blue\|sleeve=three quarter'. Keys match ^[a-z0-9_]+$, values are trimmed strings, at most 50 pairs; a duplicate key is a row error. A cell starting with '{' is parsed as a JSON object instead. Blank -> {}. |
| `image_urls` | optional | Pipe-separated absolute https:// URLs, at most 9, each <= 2048 chars and ending .jpg/.jpeg/.png/.webp (query string ignored). Position 1 becomes the main slot, the rest secondary in order. http://, data: or a relative path is a row error. Blank -> []; images can be attached later via POST /api/v1/images/upload-url. |
| `weight_grams` | optional | Integer 0 to 2,000,000, no decimals and no unit suffix (grams is in the column name). Blank -> NULL. |
| `status` | optional | One of draft \| active, lowercase. Blank -> draft. Any other value is a row error rather than a silent coercion. |
