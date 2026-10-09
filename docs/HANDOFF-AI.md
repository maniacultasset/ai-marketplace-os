# Handoff to the AI team: the generation seam (v1)

The backend owns the API, the database, jobs, adapters and the Rules Engine. The AI team owns the
`mkos_pipeline` package: the prompts, the models and the LangGraph flow. The two sides meet at
exactly one function, defined by the contracts package `mkos-schemas` at tag **`schemas-v1.0.0`**.

## 1. What you implement

```python
# mkos_pipeline.graph
async def run_listing_pipeline(ctx: GenerationContext, deps: PipelineDeps) -> GenerationResult: ...
```

- **Types.** `GenerationContext`, `GenerationResult`, `AgentBlockStatus`, `AgentRunRecord`, `CanonicalListing`,
  `ImageReport`, `ErrorReport` and `ValidationSummary` live in `mkos_schemas` (`jobs.py`, `listing.py`,
  `errors.py`). The `PipelineDeps` protocol is in `mkos_schemas/pipeline.py`.
- **How to depend on it.** Pin `mkos-schemas` by git tag (`schemas-v1.0.0`). Never copy the models.
- **Python.** 3.13: the backend runs 3.13 (ADR-0002, TEAM-QUESTIONS T1). Please confirm it.
- **Where your code lives.** Please name the repository and the tag scheme for `mkos-pipeline` (T104). The backend
  installs your tagged release in the worker image.

## 2. What the backend gives you (`PipelineDeps`)

| Port | What it does |
|---|---|
| `retrieve_brand_chunks(tenant_id, query, k=8, types=None)` | The brand's voice chunks, by vector search, scoped to the tenant. The backend embeds the query and meters it. |
| `validate_rules(listing)` | The deterministic Rules Engine (invariant 2). It is the final arbiter. It may be called at most 3 times per run; a 4th call is refused. |
| `validate_images(listing)` | The stored Pillow `ImageReport`s for the listing's images. The pipeline does not decode images. |
| `get_model_credentials(purpose)` | The model key for `generation`, `vision` or `embedding`. Use it only inside the call; never log it or put it in a prompt. |
| `emit_node_event(node, status, detail)` | One span or event per graph node, for tracing and the job's progress. |

## 3. What `GenerationContext` carries

- **Request fields:**
  - `request_id`, `job_id`, `tenant_id`, `product_id`, `listing_id`;
  - `marketplace` (`amazon` or `flipkart`), `category`;
  - `locale` (default `en-IN`).
- **`product`:** the product's facts as a plain dict (the `for_prompt()` view), each with its source.
- **`brand_profile`:** the brand profile fields, including `brand_description`.
- **`marketplace_rules`:** the `RuleSet`. **`category_template`:** the template's generation view. Adapter-only fields are stripped, and the model refuses them at the seam.
- **Optional inputs:**
  - `previous_version` (the last `CanonicalListing`);
  - `user_instructions`;
  - `review_feedback`, at most 5 items of 2,000 characters each.
- **`max_attempts`:** at most 3.
- **When the backend builds it:** when the job starts, not when it is enqueued. A retry therefore sees fresh facts.
- **What it never carries:** marketplace credentials (invariant 6), database handles, or anything that identifies another tenant.

## 4. What you return (`GenerationResult`)

- **`listing`:** a `CanonicalListing`, or `None` on failure. It holds no marketplace-specific fields (invariant 4); adapters map it later.
- **`terminal_state`, `attempts_used`** (at most 3; then `blocked`, invariant 9), `blocks`, and `error` (an `ErrorReport`, invariant 8).
- **`agent_runs`:** one `AgentRunRecord` per model call (invariant 7). The backend persists them; you never write to the database.
- **`image_reports`:** vision findings are **warnings only** in v1 (C8). There is no backend vision call; vision runs inside your pipeline and is reported here.
- **Unknown fields are ignored, not stored** (`extra="ignore"` on the seam models, T100). Inside your own package you may use `extra="forbid"` for the LLM output models.

## 5. What happens after you return

- The backend re-runs the Rules Engine on your listing.
- In one transaction, the backend writes the listing version, `validation_runs`, `agent_runs` and the status: `validated`, `needs_review`, `blocked` or `failed`.
- A human approves with maker-checker (invariant 3). **The AI never publishes** (invariant 1); only adapters publish, after approval.

## 6. How to test against the backend before your drop

- **The reference stub.** `packages/pipeline-stub` (`mkos_pipeline`) in the backend repo is the reference implementation of the seam. Its scenarios are ok, blocked, failed and vision_warn. The backend's CI and e2e flows run against it.
- **The seam contract suite.** It runs unchanged against your tagged release. If your drop passes it, the backend can swap the stub for your package.
- **The production guard.** A production worker refuses to boot with the stub installed, so the real drop is required before launch.

## 7. What we need from you

1. The repository name and the tag scheme for `mkos-pipeline` (T104).
2. Confirmation of Python 3.13 (T1).
3. A first tagged drop that implements `run_listing_pipeline` and passes the seam contract suite.
4. Any seam change. It goes through a contracts PR: additive changes become a 1.x MINOR, and breaking changes wait for the MAJOR window.
