# KNOWN ISSUES — real defects to fix at the step where they bite

On 2026-09-26 a multi-agent review checked every doc against the others and against `scaffold/`.
Each item below was confirmed by at least 2 of 3 independent reviewers. Simple "draft value → Canon
value" swaps are listed in `SUPERSEDED.md` instead. This file holds defects that need a fix, an ADR
or a client answer. When you fix an item, delete its row and cite the ADR or commit.

## A. Needs a client or product answer (ask the user; blocks the step shown)

| Issue | Why it matters | Step |
|---|---|---|
| The upstream spec site has a **Founder BRD** (11 pages) that says "default to autonomous submission after validation". It also defines a **14-status status model** and an apparel niche with variants, Legal Metrology and a 19-step flow. No repo doc mentions it. | This directly contradicts invariant 3 (human approval is mandatory) and the Canon's 9 listing statuses. The client's vision and the Canon may not match. | before S2 (S2 freezes the 9 Canon statuses (T12 default); the BRD's 14 stay open with the client) |
| Kickoff pack section B1 has copy-paste blocks that provision the **forbidden KMS ring (`aimos`/`marketplace-tokens`)** and the **singular `/v1/marketplace/...` redirect URIs**. | If the client or DevOps already ran these or filed them with Amazon or Flipkart, KMS unseal fails and the redirect URIs need a 2–4 week re-review. | now |
| No status is recorded for any Day-2–4 external item: SP-API and Flipkart filing, domain/`<APEX>`, cloud org and billing, OpenAI key, categories, UAT users. BLOCKERS.md has no status column. | The launch-critical lead times are running blind. | now |
| `rejected` is used by D13 (reviewer regenerate) and by "Reject with feedback", but it is not one of the 9 listing statuses. There is also no status for a partially published listing. | A reviewer's rejection has no persisted state. | S2, S7 (S2: no 10th status (reject → `blocked`, `ListingResult.status` `partially_published`); S7 adds the columns) |
| Emails are unique only per tenant `(tenant_id, lower(email))`, but login is by email and password with no tenant input. | The same email in two tenants makes login ambiguous. | S6, S10 |
| The ops role (who clears BLOCKED, failed-publish and DLQ items), the analytics screen contents and the confidence-score formula are all undefined. | These are product gaps with no owner. | later |

## B. Canon/Addendum design gaps (need an ADR plus a Canon edit)

| Gap | Proposed direction | Step |
|---|---|---|
| The `rules` unique index and RuleSet shadowing key omit the rule type/attribute. | Add `rule_def->>'type'` and the attribute to the key. The seed's second required attribute on `attributes` collides. | S2, S7 (S2: shadow key and resolution in the RuleSet docstring (A§17 amended); S7 the unique index) |
| Rule version selection and how `RuleSet.rules_version` is derived are unspecified. | Rule: the highest enabled version wins per key. | S7, S14 |
| `banned_terms` `match="word"`: `\b#1\b` never matches, and `*` skips the keywords fields. | Define the tokenizer, and decide whether `*` includes keywords. | S14 |
| The maker-checker 403 cannot be built as an `ErrorReport`: there is no `MAKER_CHECKER_VIOLATION` code and the field-path validator rejects non-listing paths. | Add the codes and a `request`/`listing` path for API-level errors. | S2, S10 (S2: `MAKER_CHECKER_VIOLATION` and `request`/`listing` paths added; S10 uses them) |
| `GenerationResult.terminal_state="generated"` has no mapping to a `listings.status` value. | Add an explicit mapping table. | S2, S13 (S2 freezes the 4 terminal states; the mapping is DATABASE.md:827, coded in S13/S18) |
| `ImportReport` and `JWTClaims` exist only as prose, and `export.py` doesn't emit them. | Define the models and add them to MODELS. | Fixed in S2 (`imports.py`, `auth.py`, both in `export.MODELS`). |
| The S2 module list omits `rules.py`, `pipeline.py`, `export.py` and the 4 seam DTOs. The golden fixtures don't cover all 8 contracts. | Treat the Addendum §21 list as the S2 checklist. | Fixed in S2 (the 17 A§21 modules; one golden fixture per exported model). |
| RLS 0005 conflicts with `platform_scope` (no `app.tenant_id` GUC is set). CI connects as a superuser, which bypasses RLS. | Give platform scope a BYPASSRLS role, or a policy branch. Run the isolation test as `app_rw`. | S9, 0005 |
| Rollups and cost views need `platform_scope`, but the import allowlist excludes worker code. | Extend the allowlist to a named rollup module. | S9, S14 |
| S9's `seed --reset` deletes the two seed tenants' rows with a Core delete loop on the migrator connection. S10's append-only triggers (0004) refuse DELETE on `audit_log`, `agent_runs`, `validation_runs` and `review_decisions`. | Interim: S10 A21 replaces the body of `reset_seed_tenants` with `purge_tenant(id, 'final')`; `test_seed_reset_clears_later_tables` turns red until it does. | S9, S10 |
| `app_ro` is promised, but bootstrap never creates it, and the roles get no passwords while password auth is mandated. | Extend `0000_bootstrap.sql` (secrets set outside git). | S5 |
| `audit_log.tenant_id` is NOT NULL, but platform rule changes have no tenant. | Use nullable + CHECK for `actor_type` in (system, staff), or a separate platform audit. | S7, S10 |
| `audit_log` has `ON DELETE CASCADE` from `tenants`, while 0004 makes it append-only with a DELETE-blocking trigger, and partitioning was rejected. As designed, a tenant can never be deleted, so there is no DPDP erasure path. | Write an ADR before 0004: soft offboarding (`tenants.status`) plus a migrator-owned `purge_tenant()` that the trigger recognises, with a stated audit-retention period. | 0002, 0004 |
| CODEOWNERS cannot enforce the "Tech Lead + AI lead + FE lead" approval rule, and the approval counts disagree between rules. | Use a ruleset with required approvals plus a CODEOWNERS team per path. | when GitHub Team lands (EXECUTION-PLAN decision 11; ADR-0013 §5) |
| Locally, the API and the worker health server both bind `PORT=8000`. | Give the worker health server its own `WORKER_HEALTH_PORT`, or none: a Cloud Run worker pool needs no `$PORT`. | S13 |
| The `api` entrypoint trusts forwarded headers from any peer (`--proxy-headers --forwarded-allow-ips='*'`, so `request.url` reads https behind Cloud Run). uvicorn then takes the left-most, client-supplied `X-Forwarded-For` entry as `request.client.host` (uvicorn 0.54.0 `middleware/proxy_headers.py:176-177`). | The per-IP rate limiter must derive the client IP from the entry Cloud Run appends, never the left-most one. | S10 |
| Several "Done when" lines depend on artifacts from later steps (S7, S8, S11), which creates ordering cycles. S1's part is resolved: migrations move to S5, the seed to S9, and the worker to S13 (ADR-0013 §4). | Move those tests to the later step. | planning |
| Numbering drift: the Canon cites "invariant 12" (two schemes, 10 vs 14 items); ADR numbers collide in the pack; Day 11 is a Thursday while the MAJOR windows fall on Sundays. | Use the `docs/RULES.md` §1 numbering. Keep an ADR ledger in `docs/adr/README.md`. | ongoing |

## C. Scaffold bugs (fix when the file moves into place in S1, then test)

| File | Bug | Fix |
|---|---|---|
| `app/db/session.py` | `SET LOCAL app.tenant_id = :tid` cannot be parameterized in Postgres, so **every tenant session fails**. | `SELECT set_config('app.tenant_id', :tid, true)` |
| `app/db/session.py`, `app/services/imports/row_model.py`, `app/workers/main.py`, `scripts/seed.py` | These four scaffold .py files, still unmoved, fail the canonical ruff + black run (semicolon-joined lines and more). | Reformat when moving the files. |
| `app/services/imports/row_model.py` | Before-validators run in reverse order, so blank prices become errors. `NaN`/`Infinity` prices crash with a non-ValueError. A header that passes after lowercasing still fails the exact-key model. A blank `status` fails instead of defaulting to `draft`. Short rows become `None`. A JSON attributes cell skips the key regex and the 50-pair cap. | Normalize the row once before validation, and reorder or merge the validators. Add a test per CSV contract row. |
| `app/workers/main.py` | Shutdown cancels in-flight jobs and never calls `worker.close()` / `on_shutdown`. | Drain gracefully, then close. |
| `app/workers/main.py` | Uses FastAPI + uvicorn where the Canon says an aiohttp health server. | Accept with an ADR, or switch. |
| `app/db/session.py` | Commits in dependency teardown with `autoflush=False`, so constraint errors surface outside the handler. | Flush inside the service; map IntegrityError explicitly. |
| `app/db/session.py`, `workers/main.py` | Import modules that exist nowhere (`app.core.security`, `app.db.guard`, `app.workers.settings`, `app.models`). | Create them in S9/S10/S13. Note `get_current_user` vs the Canon's `get_current_principal`. |
| `scripts/seed.py` | Creates no `category_templates`, so every CSV row fails `CATEGORY_NOT_MAPPED`. `SEED_PASSWORD` is claimed but never read. `--reset` misses the later tables. `assert` fails ruff S101. No fixture export. | Rewrite as `app/cli.py` per A§5 with these fixes. |
| `packages/schemas` (CONTRACTS reference code) | `ImageReport.model_dump()` emits `pass_`, not `pass` (needs `serialize_by_alias`). `value_submitted max_length=600` raises instead of truncating. `resolve()` rejects attribute paths with digits. The RuleDef discriminator is optional in JSON Schema. The rule message uses an undocumented `{allowed_values}` placeholder. | Fixed in S2 (`mkos-schemas` 1.0.0: `serialize_by_alias`, truncation, digit paths, required discriminator, documented placeholders). |
| `packages/pipeline/.../state.py` (AI team) | `operator.add` channels keep stale errors across correction attempts. `brand_chunks` is typed `list[str]`, not `BrandChunkDTO`. | Tell the AI team; it is their file. |
| `apps/web/eslint.config.mjs` (FE team) | Type-aware rules run on `.js` files without a project, so `eslint .` crashes. | Tell the FE team. |

## D. Hygiene

- An absolute local machine path (`…/Clients-popcom/…/scratchpad/…`) appears in the kickoff pack
  (`deliverables/`, not in git). Scrub it before sharing that file outside the team. `CHECKLISTS.md`
  was fixed on 2026-09-30.
- `BLOCKERS.md` IAM matrix gives the AI group project-level `secretAccessor`, which covers the
  marketplace secrets. That contradicts invariant 6. Grant it per secret.
- `CANON.md` was never merged with the Addendum and never mentions it. `docs/README.md` "When sources disagree" sets the
  precedence until someone merges them.
- The step that adds the S3 client must refuse `local_only_` S3 values outside `APP_ENV=local`
  (`apps/api/app/core/config.py` does this only for the JWT secret).
- The commit-msg attribution hook is bypassed by `git commit --no-verify` and by GitHub-written
  squash messages and PR bodies; a later step can run the check in the `commit-lint` CI job.
