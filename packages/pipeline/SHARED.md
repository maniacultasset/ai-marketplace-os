# Shared types before a contract merge

If `SHARED.md` and the Two-Developer Assignment section 10 differ, section 10 wins until a contract pull request changes both.

v1 pipeline pins **mkos-schemas 1.2.0**. This repo vendors a stand-in of that package under `vendor/mkos-schemas` until the Tech Lead's git tag is available.

## Seam the backend already stores

`run_listing_pipeline(ctx: GenerationContext, deps: PipelineDeps) -> GenerationResult`

Do not emit `questions` or `changes` until the Tech Lead confirms the backend persists them.

## Protocol Developer 2 implements

```python
def run_claim_check(draft, product, rules) -> ClaimCheckResult
```

`ClaimCheckResult` has `listing`, `claims`, and `removed`. The M1 graph no-ops this hook.

## Local JSON (extra=forbid)

- `ContentDraft`: title, bullets, description, claims[]
- `KeywordDraft`: search_terms, hidden_terms
- `AttributeDraft`: attributes[{key, value, source_fact}]
- `ClaimDraft`: text, fact_keys
