# mkos-pipeline

AI listing generation package for **AI Marketplace OS v1**. The backend worker calls one function per job:

```python
from mkos_pipeline import run_listing_pipeline

result = await run_listing_pipeline(ctx, deps)
```

The package never touches the database, the queue, HTTP routes, or marketplace APIs. Model calls go only to the provider named in `deps.get_model_credentials`.

## What this wheel does (M1)

| Piece | Status |
| --- | --- |
| D1-01 repository scaffold, purity + signature tests, CI | done |
| D1-02 shared `complete_json` client, cost table, safety wrapper | done |
| D1-10 graph, retries, deadline, `run_claim_check` protocol | done |
| D1-11 content agent | done |
| D1-12 keyword agent (contract caps + banned terms in code) | done |
| D1-13 `keep_attribute` / no invented values | done |
| D1-14 correction loop, cap of 3, terminal-state mapping | done |
| D1-06 live model bake-off | blocked on provider route; synthetic harness is in tests |
| D1-15 backend acceptance + tag `v0.1.0` | after Tech Lead stub-test refactor |

Developer 2 can land `prompts/registry.py` replacements, `evals/`, and `nodes/quality.py` on top of this tree. Call `complete_json`; do not add a second HTTP client.

## Install

Python 3.13. The official `mkos-schemas` git tag is not in this workspace, so a 1.2.0 stand-in lives at `vendor/mkos-schemas`. Swap it for the Tech Lead's tag before the production wheel.

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e "./vendor/mkos-schemas"
pip install -e ".[dev]"
pytest
ruff check src tests
python -m build   # sdist + wheel in dist/
```

With uv:

```bash
uv sync --extra dev
uv run pytest
uv build
```

Backend swap (do **not** commit this in the API repo):

```bash
uv pip uninstall mkos-pipeline-stub
uv pip install dist/mkos_pipeline-0.1.0-py3-none-any.whl
python -c "import mkos_pipeline; print(mkos_pipeline.__name__)"
```

A production worker refuses to boot if the installed distribution name is `mkos-pipeline-stub`. This package is `mkos-pipeline`.

## Tests

| Command | What it covers |
| --- | --- |
| `pytest tests/test_purity.py` | Forbidden imports (`fastapi`, `sqlalchemy`, …) |
| `pytest tests/test_contract.py` | `run_listing_pipeline` import path + signature |
| `pytest tests/test_llm_client.py` | timeout, invalid JSON, 500 retry, 6-decimal cost, secrets |
| `pytest tests/test_graph.py` | stage order, pass, fail-then-pass, fail×3, provider error |
| `pytest tests/test_keyword.py` | banned term dropped before `validate_rules` |
| `pytest tests/test_attribute.py` | invented values dropped; case-fold canonical form |
| `pytest tests/test_correction.py` | tiny correction prompt; 221-char title → blocked after 3 |

CI never opens a network socket. A fake transport stands in for the model.

## Terminal states we set

The backend re-runs the Rules Engine and may downgrade us. We never set `approved`.

| Remaining report | `terminal_state` |
| --- | --- |
| No listing | `failed` |
| Error-severity findings after the loop | `blocked` |
| Warnings only, or a failed agent block | `needs_review` |
| Clean, no failed blocks | `generated` |

Correction stops when fewer than 90 seconds remain on the internal 270s deadline, or after `ctx.max_attempts` (cap 3) `validate_rules` calls. A fourth call is never made.

## Model ids

Read from `credentials.model`. Nothing in `prompts/` hard-codes a model id. DeepSeek V3.2 is the meeting choice; the live route (Bedrock Mumbai vs other) is still an open Tech Lead item. Local tests use a fake transport.

## Layout

```
src/mkos_pipeline/
  graph.py                 Developer 1
  llm/                     Developer 1 builds; Developer 2 consumes
  nodes/                   Developer 1, except quality.py (Developer 2)
  nodes/fact_backed.py     keep_attribute — evals must import this, not copy it
  nodes/quality.py         run_claim_check protocol (no-op until D2-21)
  prompts/                 Developer 2 owns the registry; generation text is here
  schemas_local/           extra=forbid model-output types
  entrypoints/             stubs for later milestones
```
