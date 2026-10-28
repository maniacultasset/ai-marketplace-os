# D1-06 model check

Date: 2026-10-28.
Status: **not a live bake-off**. The Tech Lead has not confirmed a real-key route (DeepSeek direct sends data to China and is blocked unless the founder explicitly OKs it; Bedrock Mumbai is not wired yet).

This run used the synthetic G-gen items in `tests/fakes.py` and a fake transport. It does not count toward launch sign-off.

## Intended comparison (when keys exist)

| Id | Host | Role |
| --- | --- | --- |
| DeepSeek V3.2 (`deepseek-chat` / `deepseek-v3.2`) | Bedrock Mumbai if available | Primary generation |
| Qwen3 VL | Bedrock Mumbai | Vision candidate |
| Gemini 2.5 Flash-Lite | Vertex Mumbai | Vision / Flash candidate |
| GPT-4.1-mini | OpenAI | Reference only |

Prompt files must keep reading `credentials.model`. Do not hard-code these ids.

## Pricing table

`src/mkos_pipeline/llm/cost.py` is the source of truth for CI. An unknown `(provider, model)` is a hard failure, not a zero cost. Update that table in the same PR as any new model id.
