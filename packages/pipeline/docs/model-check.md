# D1-06 model check

Date: 2026-10-29.
Status: **DeepSeek chosen for v1 generation.** Live bake-off against labelled G-gen is still outstanding; this file records the product decision, not a scored 20-item run.

## Decision

| Role | Provider | Model id the backend should send | Notes |
| --- | --- | --- | --- |
| Generation (v1) | `deepseek` | `deepseek-chat` (DeepSeek V3.2) | Pipeline already speaks the OpenAI-compatible DeepSeek API. Prompt files still read `credentials.model` only. |
| Vision | undecided | n/a | Developer 2 / D1-06 follow-up. Generation does not call vision in v1. |
| Embeddings | backend-owned | n/a | We never call `purpose="embedding"`. |

Backend wiring (Tech Lead, not this package): `LLM_PROVIDER=deepseek`, `LLM_MODEL_DEFAULT=deepseek-chat`, `DEEPSEEK_API_KEY` on the worker. `get_model_credentials("generation")` must return `provider="deepseek"` and that model id.

Route still to confirm with the Tech Lead: DeepSeek **direct** (`https://api.deepseek.com/v1`, product data leaves India) versus Bedrock Mumbai (`api_base` set by the backend). The pipeline uses `credentials.api_base` when present, otherwise the DeepSeek default.

Prompt files must keep reading `credentials.model`. Do not hard-code these ids.

## Pricing table

`src/mkos_pipeline/llm/cost.py` is the source of truth for CI. An unknown `(provider, model)` is a hard failure, not a zero cost. Update that table in the same PR as any new model id.
