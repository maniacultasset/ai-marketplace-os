# V1.1 switches (SI14)

Four settings turn the V1.1 assist features on. Each ships `false` in `scripts/deploy/gcp/prod.env`
and `staging.env`, and `deploy.sh` forwards each one to the api and the worker. A switch goes on
only after its gate passes, and the user approves the change (V1.1 plan: "goes live only when the
gate passes"). The daily AI caps (prod since 2026-11-10: USD 20 platform, USD 3 per tenant) cover every call.

| Switch | What it turns on | Go-live gate |
|---|---|---|
| `CATEGORY_SUGGEST_ENABLED` | The category classifier: `POST /products/{id}/category-suggestion`, a classification queued for a product created without a category, a `category_review` task when the top confidence is below 0.80. The seller always sets the category. | G-classify, 200 products: top-1 at least 90 %, top-3 at least 98 %, at least 95 % correct when confidence is at least 0.80 |
| `CATEGORY_SUGGEST_MODEL` (`vision` or `text`, not a gate) | The classifier's model route. `vision` sends up to 6 photos to `LLM_MODEL_VISION` (gpt-4o in prod, about USD 0.008 per classification). `text` sends the product text only to `LLM_MODEL_DEFAULT` (about USD 0.0002). The 2026-11-09 live G-classify record and calibration `classify/v2` are text only on deepseek-v4-flash, so `text` is the measured configuration; `vision` has no live record (G-classify has no photo cases). | Prod runs `text` since 2026-11-10 on that record. Before `vision`, record G-classify with photos on gpt-4o and refit the calibration |
| `COLUMN_MAP_AI_ENABLED` | The AI step of `POST /imports/column-maps`: the headers the exact, alias and fuzzy rungs leave unmapped go to the pipeline's `map_columns` (never a sample cell). Off, those headers wait for the seller. | 30 real seller sheets: at least 95 % correct at confidence 0.80 or more |
| `CATEGORY_LAMPS_ENABLED` | New lamps listings (`home_decor_lamps`), lamps in the classifier's offer, and the lamps template in the requirements read. Off, an existing lamps listing still regenerates, corrects and publishes. | The read-only count below; then the 20 G-gen lamp cases pass |
| `PHOTO_QA_ENABLED` | The deterministic photo checks: fill ratio, text on the image, watermark. Always warnings, never blocking. | 100 labelled images: precision at least 90 % |
| `PHOTO_MATCH_ENABLED` (client feedback item 9; `deploy.sh` defaults it to `false` while the env files do not name it) | After a product photo passes its upload checks, one vision call (pipeline `photo_match`, mkos-pipeline 0.8.0) checks it shows this product in its colour, once per image content and colour. A `mismatch` or `unsure` photo asks the seller ("This is the right photo" keeps it, audited; or Remove). At approval an unkept `mismatch` blocks; an unkept `unsure`, a check still queued and a kept `mismatch` warn. A colour edit and each generate or regenerate queue the due checks, so photos uploaded before the switch went on (and checks that ended with no call, e.g. a budget refusal) are checked on the product's next generate: no separate backfill step. About USD 0.001 a photo. | The pipeline pin carries `photo_match` (0.8.0); G-match replay: 0 wrong photos passed as `match`, at most 20 % right photos questioned; then a live G-match record on the vision model |

## Turning a switch on

1. Confirm the gate result (the eval report from the pipeline lane, or the labelled-image run).
2. Set the switch to `true` in `prod.env` in a PR. CI's deploy meta tests keep the other switches
   as committed.
3. Deploy with `scripts/deploy/gcp/deploy.sh prod deploy --sha <sha>` (the user runs it).
4. Watch the first hour: `agent_runs` cost against the caps (`GET /admin/ai-credit`), and for the
   classifier the `category_review` tasks opened.

Turning a switch off is the same PR with `false`. A queued classification then settles
`unavailable` with no call, and a queued column map is answered with the deterministic mappings.

## The lamps count (before the first deploy of SI14)

Before SI14 reaches prod, run this read-only count against prod (the user, never an agent):

```sql
SELECT count(*) FROM products WHERE category = 'home_decor_lamps' AND deleted_at IS NULL;
```

When it is above 0, a brand already sells lamps: ship `CATEGORY_LAMPS_ENABLED=true` in the same
deploy, so their new lamps listings keep working. When it is 0, keep `false` until the G-gen lamp
cases pass.

## The India route (D13)

Every model call takes its gateway from `LLM_GATEWAY` (`direct` or `openrouter`). SI14 adds one
optional gateway per purpose: `LLM_GATEWAY_GENERATION`, `LLM_GATEWAY_VISION`,
`LLM_GATEWAY_EMBEDDING`, `LLM_GATEWAY_CLASSIFY` (the vision route) and `LLM_GATEWAY_COLUMNS` (the
generation route). Blank means `LLM_GATEWAY`; prod sets none. A worker outside local refuses to
boot when a routed purpose lacks its key, by name only.

An India-hosted provider, if the founder asks for one, is one PR:

1. a constant base URL in `app/core/config.py` (never a setting, so no env value can send a model
   call and its key elsewhere);
2. a new `LlmGateway` member and its branch in `app/services/pipeline_deps._route`;
3. possibly a new `ModelCredentials.provider` member, which is a MAJOR contract change and waits
   for a window (`docs/RULES.md` §11);
4. the same evals passing on the new route, and the data-terms review (TEAM-QUESTIONS T99).

Then set the purpose's `LLM_GATEWAY_*` in `prod.env` for the calls that should move.
