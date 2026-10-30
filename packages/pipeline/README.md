# Generation pipeline: design, flow, wiring, and cost

**Package:** `mkos-pipeline` 0.1.0  
**Lane:** Developer 1 (Generation)  
**Author:** Krishna Dahale  
**Date:** 30 October 2026  
**Audience:** Tech Lead, Developer 2, reviewers  

This document describes the AI listing generation pipeline as implemented in this package, how the backend worker calls it, what each agent does, what is complete, and how cost is recorded. It is the handover for the M1 graph. Where this file and the backend handover disagree on a code fact (name, limit, signature, or setting), the backend handover wins.

Related files in this repository: `SHARED.md`, `docs/model-check.md`. Related files outside this repository: `fosh/dev1-kickoff.md`, `fosh/pipeline-end-to-end.md`, `ai-marketplace-os/docs/handover/AI-TEAM-GUIDE.md`.

## 1. Idea

The product turns a seller's product facts, brand voice, and category template into a canonical marketplace listing (title, bullets, description, keywords, attributes). A human reviewer approves the draft. A marketplace adapter publishes it. The AI never approves and never publishes.

The AI work is one Python package. The backend worker calls one function per generation job:

```python
from mkos_pipeline import run_listing_pipeline

result = await run_listing_pipeline(ctx, deps)
```

`ctx` is a `GenerationContext` built by the worker from current database rows. `deps` is a `PipelineDeps` object whose ports the backend implements (brand retrieval, rules, image reports, model credentials, node events). The package does not import FastAPI, SQLAlchemy, the queue, or marketplace SDKs. The only outbound HTTP is the model provider named in `deps.get_model_credentials("generation")`.

The Rules Engine is the final judge. The pipeline may draft, self-correct up to three `validate_rules` calls, and return a terminal state. The worker re-runs the engine on the returned listing and maps that outcome to listing status. Auto-publish is disabled by a database constraint.

## 2. End-to-end flow

```
Seller creates or imports a product (API or dashboard)
        |
        v
POST /api/v1/listings/generate  ->  HTTP 202 { job_id, listing_id, poll_url }
        |
        v
Worker claims the job, reserves USD 0.10 of the daily budget,
reads product facts, rules, and template, builds GenerationContext
        |
        v
run_listing_pipeline(ctx, deps)
        |
        v
Worker trust-checks the GenerationResult, re-runs the Rules Engine,
persists one listing version, agent_runs, and validation_runs,
replaces the reservation with the sum of recorded cost_usd
        |
        v
Listing status: validated | needs_review | blocked | failed
        |
        v
Human review, then adapter publish. The pipeline is not on that path.
```

Inside `run_listing_pipeline` the graph is fixed. Nodes listed on one line run in parallel.

| Order | Node | Parallelism | Role |
| --- | --- | --- | --- |
| 1 | `load_context` | sequential | Accepts locale `en-IN` only. Copies ids onto graph state. |
| 2 | `retrieve_brand` | three facets in parallel | Vector search via `deps.retrieve_brand_chunks`: tone and example (k=6), glossary (k=4), compliance (k=4). The backend owns embeddings. |
| 3 | `content_agent`, `keyword_agent`, `attribute_agent` | three agents in parallel | Draft copy, search terms, and fact-backed attributes. |
| 4 | `image_agent` | sequential | Attaches stored `ImageReport`s. No vision model in v1 generation. |
| 5 | `merge_outputs` | sequential | Builds one `CanonicalListing`. Drops invented attributes. |
| 6 | `validate_schema` | sequential | Pydantic check. One JSON repair retry that does not count toward the three-attempt cap. |
| 7 | `validate_rules` | sequential | `deps.validate_rules(draft)`. Each call increments `attempts_used`. Cap of 3. |
| 8 | `validate_vision` | sequential | Stores vision reports if present. Does not change terminal status in v1. |
| 9 | `correct` | sequential | Re-runs only the owning agent for remaining error-severity fields. Stops at the attempt cap or when fewer than 90 seconds remain on the internal deadline. |
| 10 | `quality_check` | sequential | Calls `run_claim_check`. No-op protocol until Developer 2 lands D2-21. |

The function returns a `GenerationResult`: listing (or none), image reports, up to 100 `AgentRunRecord`s with cost, per-agent blocks, `attempts_used`, and one of `generated`, `needs_review`, `blocked`, `failed`.

### 2.1 How the backend maps that result

The worker never trusts the pipeline's terminal state alone. After a second Rules Engine pass:

| Condition | Listing status | `status_reason` |
| --- | --- | --- |
| No listing, or pipeline `failed` | `failed` | `generation_failed` |
| Re-check has error-severity findings | `blocked` | `corrections_exhausted` |
| Warnings only, a failed agent block, or `needs_review` | `needs_review` | none |
| Re-check is clean | `validated` | none |

The listing never moves to `approved` from generation.

## 3. Agents

All three generation agents share one HTTP client (`complete_json`), one system prefix, and one cost table. Prompts live under `src/mkos_pipeline/prompts/<name>/v1.md`. Prompt files do not contain a model id. The model id comes from `credentials.model`.

Untrusted text (seller instructions, review feedback, brand chunks, product strings) is always last, inside a `<data>` block. The system prefix is first so the provider can cache it.

### 3.1 Content agent (`nodes/content.py`)

**Input:** product identity, attributes, brand chunks (tone, glossary, compliance), compact rules, banned terms, user instructions, previous version content if any.  
**Output:** `ContentDraft`: title, bullets, description, and optional claims.  
**Prompt:** `prompts/content/v1.md`.  
**Guards:** claims without a backing fact key are dropped. Banned promotional language is listed in the prompt and in code. Title is truncated to 500 characters at merge; description to 5,000.

### 3.2 Keyword agent (`nodes/keyword.py`)

**Input:** identity, attributes, brand chunks, compact rules, banned terms.  
**Output:** `KeywordDraft`: `search_terms` and `hidden_terms`.  
**Prompt:** `prompts/keyword/v1.md`.  
**Guards:** contract caps (50 terms, 256 characters each) and global banned terms are enforced in code. A banned term in model JSON is dropped before `validate_rules`. The Tech Lead seeded a `hidden_terms` byte-limit rule and intentionally did not seed a `search_terms` rule.

### 3.3 Attribute agent (`nodes/attribute.py`, `nodes/fact_backed.py`)

**Input:** category template (required, optional, conditional, specs), product attributes, extracted candidates from facts.  
**Output:** `AttributeDraft`: list of `{key, value, source_fact}`.  
**Prompt:** `prompts/attribute/v1.md`.  
**Guards:** `keep_attribute` / `canonical_attribute_value` drop any value that is not in the product facts or not in the template's allowed values. The agent does not invent `color`, `fabric_type`, or other required attributes. If the model call fails, the node falls back to deterministic extraction from facts.

A product created with only SKU and title will therefore generate copy and keywords, then fail Amazon required-attribute rules (`MISSING_ATTRIBUTE` on `attributes.color` and `attributes.fabric_type`). That is intended. The seller must supply those facts. The live run `OR-DEMO-002` demonstrated this path (`blocked` / `corrections_exhausted`). The live run `OR-DEMO-003` supplied `color=indigo` and `fabric_type=cotton` and ended `validated`.

### 3.4 Image agent (`nodes/image.py`)

Does not call a model. It copies image refs from `previous_version` (excluding `packaging_label`) and asks `deps.validate_images` for stored reports. On a first generate with no previous version the block is `skipped`. Developer 2 owns vision extraction (D2-32).

### 3.5 Correction (`nodes/correct.py`)

After the first `validate_rules` call, remaining error-severity items are grouped by owner (`content.`, `keywords.`, `attributes`). Only those agents re-run, with a short payload: field, error code, message, suggestion, previous value. Correction prompts are `prompts/*_correct/v1.md`. The loop stops when the report is clean, `attempts_used` reaches `min(ctx.max_attempts, 3)`, fewer than 90 seconds remain, merge fails, or no remaining error has an owning agent (for example `images`).

### 3.6 Quality check (`nodes/quality.py`)

Exports `run_claim_check(draft, product, rules) -> ClaimCheckResult`. The current implementation is a no-op that returns the draft unchanged. Developer 2 replaces this in D2-21 without changing the graph.

## 4. Implementation status (30 October 2026)

Kickoff window: D1-01 through D1-15, due 4 November 2026. Carry-overs start at D1-20.

| Task | Status | Evidence |
| --- | --- | --- |
| D1-01 repository scaffold, purity and signature tests | Done | `tests/test_purity.py`, `tests/test_contract.py` |
| D1-02 LLM client, cost table, deadline, safety wrapper | Done | `llm/client.py`, `llm/cost.py`, `tests/test_llm_client.py` |
| D1-06 live bake-off on labelled G-gen | Not done | Synthetic harness only. Production provider route (Bedrock Mumbai versus founder approval for a non-India route) is still open. Local OpenRouter is a developer workstation path, not that decision. |
| D1-10 graph, retries, deadline, claim-check protocol | Done | `graph.py`, `tests/test_graph.py` |
| D1-11 content agent | Done | Live listing `aea4b94d-8602-4289-957b-a51769dcf252` title "Cotton Kurta Set in Indigo" |
| D1-12 keyword agent | Done | Live `search_terms` / `hidden_terms` on that listing |
| D1-13 fact-backed attributes | Done | `tests/test_attribute.py`; `OR-DEMO-003` attributes `Indigo` / `Cotton` |
| D1-14 correction loop | Done | `OR-DEMO-002` used two correction calls, then `blocked` after missing facts |
| D1-15 backend acceptance, wheel `v0.1.0`, stub-test refactor | Not done | Local install is editable `--no-deps` against backend schemas 2.0.0. The Dockerfile still installs `mkos-pipeline-stub`. Tag is not cut. |
| D1-20 claims and `attribute_sources` | Carry-over | Schema fields exist. Pipeline does not emit them yet. |
| D1-23 Flipkart tuning | Carry-over | Needs Developer 2's G-gen growth (D2-22). |
| D1-34 / D1-35 questions and partial regeneration | Carry-over | Needs the answer endpoint and `answered_keys`. |
| D1-40 / D1-41 marketplace error explainer and value-map proposals | Carry-over | Needs Tech Lead contracts and tables. |
| D1-51 cost and latency live eval | Carry-over | Needs held-out G-gen. Local costs are recorded below. |
| D-52 pilot diffs | Carry-over | After M1 to M4. |

Developer 2 seams that this package already exports: `complete_json`, `load_prompt` / `prompt_version`, `keep_attribute`, `run_claim_check`. Developer 2 should not add a second HTTP client.

## 5. Wiring

### 5.1 Contract

| Direction | Type | Module |
| --- | --- | --- |
| In | `GenerationContext` | `mkos_schemas.jobs` |
| In | `PipelineDeps` | `mkos_schemas.pipeline` |
| Out | `GenerationResult` | `mkos_schemas.jobs` |
| Out | `AgentRunRecord` | `mkos_schemas.agent` |

The worker that imports this package is `apps/api/app/workers/generation.py` in `ai-marketplace-os`. It is the only backend module allowed to import `mkos_pipeline`.

Ports used:

| Port | Backend implementation | Pipeline use |
| --- | --- | --- |
| `retrieve_brand_chunks` | Embed query, then `BrandChunkRepository.search` | `retrieve_brand` |
| `validate_rules` | Rules Engine on the preloaded `RuleSet` | `validate_rules` and the correction loop |
| `validate_images` | Stored image reports, no extra model call | `image_agent`, `validate_vision` |
| `get_model_credentials` | Snapshot of `LLM_PROVIDER`, key, model, optional `LLM_API_BASE` | Every `complete_json` call |
| `emit_node_event` | Writes `job_runs.node` on `started`; logs redacted detail | Every graph node |

The pipeline does not read `os.environ` for keys. The worker passes `ModelCredentials` through the port.

### 5.2 Schemas

This package vendors `mkos-schemas` 1.2.0 (`vendor/mkos-schemas`). The backend workspace currently ships `mkos-schemas` 2.0.0. Names that moved (block status, token usage, attribute spec, category template, some enums) are aliased in `src/mkos_pipeline/schema_compat.py`. Rule objects in 2.0.0 store type and limit on a discriminated `rule_def` model rather than `rule_type` plus a dict; `product.py` reads both shapes.

A production wheel should pin one schemas version. The shim exists so a local editable install can run against the backend 2.0.0 extra without replacing that extra (`uv pip install --no-deps -e ../fosh/mkos-pipeline` after uninstalling `mkos-pipeline-stub`).

Error items use field path `listing` (a legal 2.0.0 path). Field `*` is valid on rules, not on `ErrorItem`.

### 5.3 Local application wiring (workstation only)

These settings are local. They are not a production routing decision.

| Setting | Local value used for the 30 October demo |
| --- | --- |
| `LLM_PROVIDER` | `deepseek` |
| `LLM_MODEL_DEFAULT` | `deepseek/deepseek-chat` (OpenRouter model id) |
| `LLM_API_BASE` | `https://openrouter.ai/api/v1` |
| `DEEPSEEK_API_KEY` | OpenRouter key, never committed |
| Worker | `APP_ROLE=worker PYTHONPATH=apps/api .venv/bin/python -m app.workers.main` |

`make worker` and `uv sync --frozen` reinstall the stub. After `make dev`, swap the editable pipeline and start the worker with the command above. Do not print the key.

`get_model_credentials("generation")` forwards `LLM_API_BASE` as `credentials.api_base`. Vision and embedding ignore it and stay on OpenAI. If `api_base` is unset, the client uses `https://api.deepseek.com/v1`.

Production routing remains open: Bedrock Mumbai (preferred in the model-choice note) versus founder approval for a non-India route. OpenRouter on a developer machine does not close that item.

### 5.4 Node events and jobs

`emit_node_event(..., "started")` is the last write to `job_runs.node`. The three agents emit `started` before `asyncio.gather`. If any of them raises before completion, the job can show `stage=attribute_agent` even when the failing code was content or keyword. That is how the first local failure presented, before the 2.0.0 rule-shape shim.

## 6. Cost accounting

### 6.1 Formula

`src/mkos_pipeline/llm/cost.py` is the source of truth for CI. An unknown `(provider, model)` raises `UnknownModelPricing` and fails the job. Silent zero cost is forbidden.

```
cost_usd = billed_input * input_rate / 1e6
         + cached_input * cached_rate / 1e6
         + output * output_rate / 1e6
```

`billed_input = max(input_tokens - cached_input_tokens, 0)`. The result is quantized to six decimal places, `ROUND_HALF_UP`, matching `agent_runs.cost_usd NUMERIC(12,6)`.

Rates in the table (USD per million tokens):

| Provider | Model id | Input | Output | Cached input |
| --- | --- | --- | --- | --- |
| deepseek | `deepseek-chat` | 0.27 | 1.10 | 0.07 |
| deepseek | `deepseek/deepseek-chat` | 0.27 | 1.10 | 0.07 |
| deepseek | `deepseek-v3.2` | 0.27 | 1.10 | 0.07 |
| deepseek | `deepseek-reasoner` | 0.55 | 2.19 | 0.14 |
| openai | `gpt-4o-mini` | 0.15 | 0.60 | 0.075 |
| openai | `gpt-4.1-mini` | 0.40 | 1.60 | 0.10 |
| openai | `gpt-4.1` | 2.00 | 8.00 | 0.50 |

The OpenRouter slug is aliased to the same DeepSeek chat rates so local runs do not hard-fail. Native DeepSeek list prices were the design source. OpenRouter's public list on 30 October 2026 for `deepseek/deepseek-chat` was about USD 0.2574 input and USD 1.029 output per million tokens. The table is therefore slightly conservative versus that invoice (about 5 percent on input, 7 percent on output). Reconcile the table against the production provider when the route is chosen.

Token usage is taken from the provider `usage` object. A response that omits usage is recorded as failed (`GENERATION_FAILED`) and does not invent a cost. Cached tokens are read from `usage.prompt_tokens_details.cached_tokens` when present.

### 6.2 Budget

The worker reserves USD 0.10 per generation claim (`GENERATION_RESERVATION_USD`). Caps are `LLM_DAILY_BUDGET_USD` (platform) and `LLM_TENANT_DAILY_BUDGET_USD`. On a clean finish the reservation is zeroed and replaced by the sum of persisted `agent_runs.cost_usd`. On a crash the reservation stays counted so spend is not under-counted.

The product bar is median at most USD 0.03 per listing per marketplace, p95 latency at most 120 seconds.

### 6.3 Live local measurements (30 October 2026)

Provider: OpenRouter, model `deepseek/deepseek-chat`. Costs below are what `compute_cost` wrote to `agent_runs`, using the table in 6.1. Embeddings are backend-owned and recorded separately at USD 0.000000 with `text-embedding-3-small`.

**OR-DEMO-003** (facts present, listing `validated`, job succeeded in one `validate_rules` pass):

| Agent | Trigger | Input tokens | Output tokens | Cached | `cost_usd` |
| --- | --- | --- | --- | --- | --- |
| attribute | generate | 980 | 73 | 0 | 0.000345 |
| keyword | generate | 533 | 113 | 0 | 0.000268 |
| content | generate | 801 | 148 | 0 | 0.000379 |
| **Total** | | **2,314** | **334** | **0** | **0.000992** |

Worked example (content row): `801 * 0.27 / 1e6 + 148 * 1.10 / 1e6 = 0.000379`. Matches the stored value.

**OR-DEMO-002** (no color or fabric facts, listing `blocked` after corrections):

| Agent | Trigger | Input tokens | Output tokens | Cached | `cost_usd` |
| --- | --- | --- | --- | --- | --- |
| attribute | generate | 932 | 6 | 0 | 0.000258 |
| content | generate | 789 | 124 | 0 | 0.000349 |
| keyword | generate | 521 | 133 | 0 | 0.000287 |
| correction | correct | 396 | 6 | 0 | 0.000114 |
| correction | correct | 396 | 6 | 0 | 0.000114 |
| **Total** | | **3,034** | **275** | **0** | **0.001122** |

Both jobs are well under the USD 0.03 median bar (about 3 percent of the bar, about 1 percent of the USD 0.10 reservation). Wall time for the validated job was on the order of 7 to 8 seconds, well under the 120 second p95 bar.

Cached input was 0 on every generation call. The shared system prefix is in place, but OpenRouter did not report `cached_tokens` on these requests. Confirm cache reporting on the production provider. Until then, billed input is the full prompt.

### 6.4 Cost controls already in the graph

1. One shared system prefix; untrusted data last.  
2. Correction prompts carry only the failing field, not the full catalogue.  
3. Hard cap of three `validate_rules` calls.  
4. Stop corrections when fewer than 90 seconds remain.  
5. No fourth brand-retrieval facet.  
6. No vision call in v1 generation.  
7. CI uses a fake transport. No network in pytest.  
8. Unknown model ids fail closed.

## 7. Security boundaries

1. The package has no database session, no queue client, and no marketplace credentials.  
2. Prompts never include API keys. `ModelCredentials.api_key` is a `SecretStr`. Logs and exception text are asserted not to contain the test key.  
3. The backend secret walk refuses a `GenerationContext` whose product or brand dicts contain denied key names (`api_key`, `password`, `access_token`, and the rest of the deny list).  
4. The client talks only to `credentials.api_base` or the provider default. `api_base` must be https, without userinfo, and not a private host (enforced on the backend `ModelCredentials` type).  
5. One tenant per call. Tenant id is taken from `ctx.tenant_id`, never from prompt text.

## 8. Remaining Developer 1 work

### 8.1 Still inside the 1.5-week window (through 4 November 2026)

1. **D1-06.** Run the labelled G-gen bake-off once the production provider route is confirmed. Record scores in `docs/model-check.md`. Local OpenRouter is not a substitute for that decision.  
2. **D1-15.** Build the wheel, pass the backend acceptance, prompt-injection, and secrets-never-logged tests against the installed distribution (not an editable `--no-deps` tree). Tag `v0.1.0`. The Tech Lead refactors stub-tied tests and swaps the Docker image.  
3. **Production credentials.** Agree with the Tech Lead and founder: Bedrock Mumbai, or an explicit founder OK for a non-India route. Set zero data retention on the chosen route.  
4. **Schemas pin.** Replace `schema_compat.py` with a single schemas version before the production wheel. Prefer the backend's 2.0.0 extra if that is what the worker will run.

### 8.2 After 4 November (named carry-overs)

D1-20 claims and attribute sources. D1-23 Flipkart tuning. D1-34 questions. D1-35 partial regeneration. D1-40 marketplace error explainer. D1-41 value-map proposals. D1-51 cost and latency live eval on held-out G-gen. D-52 pilot diffs (content prompt fixes).

### 8.3 Depends on other lanes

| Need | Owner |
| --- | --- |
| Prompt registry wording (D2-03) | Developer 2 |
| Claim checker body (D2-21) | Developer 2 |
| Labelled G-gen and held-out split (D2-05, D2-22) | Developer 2, client data |
| Stub-test refactor and wheel swap (BE-04) | Tech Lead |
| Production `LLM_*` secrets and provider route | Tech Lead, founder |

## 9. How to read a successful local run

Staff admin: `/internal/admin` (local only). The listing to cite for the 30 October demo is `aea4b94d-8602-4289-957b-a51769dcf252` (SKU `OR-DEMO-003`), status `validated`, version `4a280682-ba2c-4ecc-82fc-b2d0ec813b01`. Agent runs on that listing show `model_used=deepseek/deepseek-chat` and non-zero `cost_usd`. Seed listings (`KURTA-001`, `KURTA-FAM-01`) are from `make seed` and are not this generate.

A generate against a product that lacks required attributes is expected to end `blocked`. That is the Rules Engine doing its job, not a pipeline outage.

## 10. Install and tests

Python 3.13. A 1.2.0 stand-in lives at `vendor/mkos-schemas` until the production wheel pins the Tech Lead's tag.

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e "./vendor/mkos-schemas"
pip install -e ".[dev]"
pytest
ruff check src tests
```

CI never opens a network socket. A fake transport stands in for the model.
