# KNOWN ISSUES — real defects to fix at the step where they bite

On 2026-09-26 a multi-agent review checked every doc against the others and against `scaffold/`.
Each item below was confirmed by at least 2 of 3 independent reviewers. Simple "draft value → Canon
value" swaps are listed in `SUPERSEDED.md` instead. This file holds defects that need a fix, an ADR
or a client answer. When you fix an item, delete its row and cite the ADR or commit.

## A. Needs a client or product answer (ask the user; blocks the step shown)

| Issue | Why it matters | Step |
|---|---|---|
| The upstream spec site has a **Founder BRD** (11 pages) that says "default to autonomous submission after validation". It also defines a **14-status status model** and an apparel niche with variants, Legal Metrology and a 19-step flow. No repo doc mentions it. | This directly contradicts invariant 3 (human approval is mandatory) and the Canon's 9 listing statuses. The client's vision and the Canon may not match. | before S2 (S2 freezes the 9 Canon statuses (T12 default); the BRD's 14 stay open with the client) |
| Kickoff pack section B1 has copy-paste blocks that provision the **forbidden KMS ring (`aimos`/`marketplace-tokens`)** and the **singular `/v1/marketplace/...` redirect URIs**. | If the client or DevOps already ran these or filed them with Amazon or Flipkart, KMS unseal fails and the redirect URIs need a 2–4 week re-review. | now (S15: fixed in code: a deployed api or worker refuses a `KMS_KEY_URI` naming the `aimos` ring or the `marketplace-tokens` key, `apps/api/app/core/config.py`; the filed redirect URIs are still to check, T77) |
| No status is recorded for any Day-2–4 external item: SP-API and Flipkart filing, domain/`<APEX>`, cloud org and billing, OpenAI key, categories, UAT users. BLOCKERS.md has no status column. | The launch-critical lead times are running blind. | now |
| `rejected` is used by D13 (reviewer regenerate) and by "Reject with feedback", but it is not one of the 9 listing statuses. There is also no status for a partially published listing. | A reviewer's rejection has no persisted state. | S2, S7 (S2: no 10th status (reject → `blocked`, `ListingResult.status` `partially_published`); S7 adds the columns) |
| Emails are unique only per tenant `(tenant_id, lower(email))`, but login is by email and password with no tenant input. | The same email in two tenants makes login ambiguous. | S6, S10 |
| The ops role (who clears BLOCKED, failed-publish and DLQ items), the analytics screen contents and the confidence-score formula are all undefined. | These are product gaps with no owner. | later |
| `banned_terms` normalisation (S14a PLAN D3a: NFKC, casefold, whitespace collapse) keeps format characters (Cf: U+200B, U+00AD, U+2060, U+FEFF), so `be\u200bst` passes a `best` ban. The site asks for an exact match, not a semantic one (system-arch-llms-full.txt L11559). | A seller or a model can slip a banned claim past the engine with an invisible character. Proposed: drop default-ignorable Cf characters after NFKC in `normalise`, except ZWJ/ZWNJ (U+200C/U+200D), which change Devanagari rendering; needs a D3a ruling. | S14b (before tenant banned-term rows) |

## B. Canon/Addendum design gaps (need an ADR plus a Canon edit)

| Gap | Proposed direction | Step |
|---|---|---|
| The `rules` unique index and RuleSet shadowing key omit the rule type/attribute. | Add `rule_def->>'type'` and the attribute to the key. The seed's second required attribute on `attributes` collides. | S2, S7 (S2: shadow key and resolution in the RuleSet docstring (A§17 amended); S7 the unique index) |
| `check_rule_regex` (rules.py:126) accepts adjacent overlapping unbounded repeats (`\s*\s*x`), and the rules.py:131-132 docstring promises RE2 with a timeout that does not exist. | S14a refuses them in `compile_rule_regex` (D6 B); tighten the contract gate in a later MINOR; google-re2 (D6 A) before the T29 rules-pack import or S14b dry-run accepts `pattern`/`regex` rows. Also: stdlib `re.compile` itself costs ~11 ms (~37 ms with IGNORECASE) per term with many wide ranges, so 500 such terms compile in ~6-25 s cold; RE2 or a per-term class-span cap fixes it. | S14b, MINOR |
| Fact rules not evaluated in v1: `fact_source`/`fact_backed_attribute` are skipped (`NOT_EVALUATED`), rule fields and `cross_field` sides on `NOT_LISTING_ROOTS` give no item, and `MISSING_ATTRIBUTE` stays `user_action` (T47). | No step ships `mkos_schemas/facts.py` in v1 (S16 does not; S18 uses an internal read model). | post-v1 (`facts.py` contract MINOR; no v1 owner) |
| The maker-checker 403 cannot be built as an `ErrorReport`: there is no `MAKER_CHECKER_VIOLATION` code and the field-path validator rejects non-listing paths. | Add the codes and a `request`/`listing` path for API-level errors. | S2, S10 (S2: `MAKER_CHECKER_VIOLATION` and `request`/`listing` paths added; S10 uses them) Fixed at the database in S10a: `lv_guard` raises `MKC01`, mapped to `MAKER_CHECKER_VIOLATION` (`app/db/integrity.py`); the API 403 is S19. |
| `GenerationResult.terminal_state="generated"` has no mapping to a `listings.status` value. | Add an explicit mapping table. | S2, S13 (S2 freezes the 4 terminal states; the mapping is DATABASE.md:827, coded in S13/S18) |
| `ImportReport` and `JWTClaims` exist only as prose, and `export.py` doesn't emit them. | Define the models and add them to MODELS. | Fixed in S2 (`imports.py`, `auth.py`, both in `export.MODELS`). |
| The S2 module list omits `rules.py`, `pipeline.py`, `export.py` and the 4 seam DTOs. The golden fixtures don't cover all 8 contracts. | Treat the Addendum §21 list as the S2 checklist. | Fixed in S2 (the 17 A§21 modules; one golden fixture per exported model). |
| RLS 0005 conflicts with `platform_scope` (no `app.tenant_id` GUC is set). CI connects as a superuser, which bypasses RLS. | Give platform scope a BYPASSRLS role, or a policy branch. Run the isolation test as `app_rw`. | S9, 0005 |
| Rollups and cost views need `platform_scope`, but the import allowlist excludes worker code. | Extend the allowlist to a named rollup module. | S9, S14, S23. Cost views fixed in S14b (`app/admin/ops.py` reads through `platform_scope`, allowed under `app/admin/**`); the S13 reaper and retention loop use it from `app/workers/periodic.py`. Open: the S23 worker rollups. |
| S9's `seed --reset` deletes the two seed tenants' rows with a Core delete loop on the migrator connection. S10's append-only triggers (0004) refuse DELETE on `audit_log`, `agent_runs`, `validation_runs` and `review_decisions`. | Interim: S10 A21 replaces the body of `reset_seed_tenants` with `purge_tenant(id, 'final')`; `test_seed_reset_clears_later_tables` turns red until it does. | S9, S10 Fixed in S10a: `reset_seed_tenants` runs `purge_tenant(id, 'data')` then `'final'`. |
| `app_ro` is promised, but bootstrap never creates it, and the roles get no passwords while password auth is mandated. | Extend `0000_bootstrap.sql` (secrets set outside git). | S5 |
| `audit_log.tenant_id` is NOT NULL, but platform rule changes have no tenant. | Use nullable + CHECK for `actor_type` in (system, staff), or a separate platform audit. | S7, S10 Fixed in S10a: `audit()` and the actor CHECKs accept a NULL tenant for `system`/`staff`. |
| `audit_log` has `ON DELETE CASCADE` from `tenants`, while 0004 makes it append-only with a DELETE-blocking trigger, and partitioning was rejected. As designed, a tenant can never be deleted, so there is no DPDP erasure path. | Write an ADR before 0004: soft offboarding (`tenants.status`) plus a migrator-owned `purge_tenant()` that the trigger recognises, with a stated audit-retention period. | 0002, 0004 Fixed in S10a: ADR-0014 (proposed) and `purge_tenant()`, which the append-only triggers recognise (`session_user = migrator` + `app.purge_tenant`). |
| CODEOWNERS cannot enforce the "Tech Lead + AI lead + FE lead" approval rule, and the approval counts disagree between rules. | Use a ruleset with required approvals plus a CODEOWNERS team per path. | when GitHub Team lands (EXECUTION-PLAN decision 11; ADR-0013 §5) |
| Locally, the API and the worker health server both bind `PORT=8000`. | Give the worker health server its own `WORKER_HEALTH_PORT`, or none: a Cloud Run worker pool needs no `$PORT`. | S13 Fixed in S13a: the worker binds no port and has no health server (`APP_ROLE=worker`, CANON.md row 63 superseded by row 25). |
| The `api` entrypoint trusts forwarded headers from any peer (`--proxy-headers --forwarded-allow-ips='*'`, so `request.url` reads https behind Cloud Run), and uvicorn takes the left-most, client-supplied `X-Forwarded-For` entry as `request.client.host` (uvicorn 0.54.0 `middleware/proxy_headers.py:176-177`). **S10c built the fix** (`app/api/rate_limit.py`): the limiter never reads `request.client` behind a proxy; it takes the raw header's entry `HOPS[APP_ENV]` places from the right: `local` 0 (no proxy: the ASGI peer), `production` 2 (the global external ALB appends `<client>,<lb>`), `staging` 2 (Firebase Hosting rewrite, not yet observed). **Residuals:** both hop counts must be checked on the first real request through each ingress; on the local image the peer is uvicorn's rewritten left-most entry (ingress is `127.0.0.1` only, ARCH.md); a request that reaches the Cloud Run URL directly, bypassing the ALB, would let a caller pick its bucket until the Cloud Run ingress setting closes that path. | Verify the staging hop at the first staging deploy and the production hop with ingress `internal-and-cloud-load-balancing`; change only the `HOPS` row if either differs. | S12 (staging), S25 (prod) |
| Several "Done when" lines depend on artifacts from later steps (S7, S8, S11), which creates ordering cycles. S1's part is resolved: migrations move to S5, the seed to S9, and the worker to S13 (ADR-0013 §4). | Move those tests to the later step. | planning |
| Numbering drift: the Canon cites "invariant 12" (two schemes, 10 vs 14 items); ADR numbers collide in the pack; Day 11 is a Thursday while the MAJOR windows fall on Sundays. | Use the `docs/RULES.md` §1 numbering. Keep an ADR ledger in `docs/adr/README.md`. | ongoing |

## B2. Open after S13b

| Item | State | Next |
|---|---|---|
| `facts_guard` PRV04 (a changed typed value needs a changed `provenance.<key>.updated_at`) and PRV05 (a party used as manufacturer … consumer care lists that role and is live), DATABASE.md:1184-1185 | Revision `0007_facts_guard` (S13b) ships PRV01-PRV03 only (CODE-PLAN S13 Q13); no step owns PRV04/PRV05 yet. The FactWriter always writes the provenance with the value, and the import's parties carry their role, so v1 writers keep both rules by construction. | Unowned: name the step (a new revision) before S18 writes facts from the AI. |

## C. Scaffold bugs (fix when the file moves into place in S1, then test)

| File | Bug | Fix |
|---|---|---|
| `app/db/session.py` | `SET LOCAL app.tenant_id = :tid` cannot be parameterized in Postgres, so **every tenant session fails**. | `SELECT set_config('app.tenant_id', :tid, true)` |
| `app/db/session.py`, `app/services/imports/row_model.py`, `app/workers/main.py`, `scripts/seed.py` | These four scaffold .py files, still unmoved, fail the canonical ruff + black run (semicolon-joined lines and more). | Reformat when moving the files. Fixed in S13b for the last two: the scaffold `row_model.py` and `workers/main.py` are deleted; the real modules pass ruff and black. |
| `app/services/imports/row_model.py` | Before-validators run in reverse order, so blank prices become errors. `NaN`/`Infinity` prices crash with a non-ValueError. A header that passes after lowercasing still fails the exact-key model. A blank `status` fails instead of defaulting to `draft`. Short rows become `None`. A JSON attributes cell skips the key regex and the 50-pair cap. | Normalize the row once before validation, and reorder or merge the validators. Add a test per CSV contract row. Fixed in S13b: `normalize_row` runs once (trim, collapse, blank → None, alias → canonical) before one validation pass with no before-validators; NaN/Infinity, 3 decimals and out-of-range prices are row errors; a lower-cased header and alias match; there is no `status` column (always `draft`); short rows pad with `""`; a JSON-looking cell is plain text (one test each, `tests/unit/test_row_model.py`). |
| `app/workers/main.py` | Shutdown cancels in-flight jobs and never calls `worker.close()` / `on_shutdown`. | Drain gracefully, then close. Fixed in S13a: SIGTERM gives running jobs 8 s, releases the rest uncounted, unregisters the workers and closes the queue app (ADR-0017). |
| `app/workers/main.py` | Uses FastAPI + uvicorn where the Canon says an aiohttp health server. | Accept with an ADR, or switch. Fixed in S13a: no health server at all (CANON.md row 63 superseded by row 25). |
| `app/db/session.py` | Commits in dependency teardown with `autoflush=False`, so constraint errors surface outside the handler. | Flush inside the service; map IntegrityError explicitly. Import rows also flush inside their own SAVEPOINT (S13b `row_savepoint`), so a row's constraint error is that row's result, mapped by constraint name. |
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
  (`apps/api/app/core/config.py` does this only for the JWT secret). Fixed in S13b: a deployed api
  or worker refuses committed or dev-prefixed `S3_ACCESS_KEY`/`S3_SECRET_KEY` and an http or loopback
  `S3_ENDPOINT_URL`. S17b adds the same refusal in the storage client itself (`get_storage()`,
  any role): blank keys, `local_only_` values in any case or padding, and a non-https endpoint.
- The commit-msg attribution hook is bypassed by `git commit --no-verify` and by GitHub-written
  squash messages and PR bodies; a later step can run the check in the `commit-lint` CI job.

## E. Known gaps carried to a later step

- **Orphaned `queued` job after a SIGKILL (S13a, LOW; fix in S26 failure injection).** A worker
  killed after `release_retry` / `release_shutdown` commits, but before procrastinate re-queues the
  entry, leaves the `job_runs` row `queued` and its `procrastinate_jobs` entry `doing`. `reap_once`
  (`app/workers/periodic.py`) reads only expired `running` rows, so that job never runs or ends.
  Fix: a reaper sweep that re-defers `queued` rows whose entry belongs to a pruned worker, with a
  kill-at-that-point test. A graceful SIGTERM does not hit this (ADR-0017 drain).
- **Job-spine follow-ups from the S13a review (LOW; S26 unless a later step hits them first).**
  Periodic tasks share the 4-slot `default` queue with `bulk_import` (1,800 s timeout), so four
  long imports can delay the reaper up to 30 min (fix: a `periodic` queue). An exception in the
  terminal write leaves the row `running` until its lease expires, then the body runs again (fix:
  record `failed`/`INTERNAL_ERROR` in a second fenced transaction). `retention_daily` holds one
  transaction per tenant, not per 1,000-row batch. Rows whose reap keeps failing are skipped per job
  (F1), but 500 of them would fill the oldest-lease page (fix: push the lease forward on failure).
- **AI-seam follow-ups from the S14b review (LOW).** `JobRunRepository.set_node` fences on status,
  not on the claimed attempt, so a reaped attempt 1 can overwrite attempt 2's `node` (fix in S18:
  pass the attempt to `_fence`). `BackendPipelineDeps.validate_rules` runs the synchronous rules
  engine on the worker's event loop (fix in S18: `anyio.to_thread.run_sync` with a shared limiter,
  as the API does). `seed_ops.py` uses `on_conflict_do_nothing()` without a target, so a foreign
  listing on the same SKU skips the insert and the version insert then fails its FK (local seed
  only). API CORS also answers preflights on `/internal/admin` (the gate still refuses the GET).
  `FakeAdapter.update_listing` on a path the listing lacks raises a raw `KeyError`.
- **CSV-import follow-ups from the S13b review (LOW).** A row of only commas fails instead of
  being skipped as blank (`csv_file.py`). On a rows retry, a row the parser refused (`raw_row {}`)
  loses its real error and shows four `REQUIRED_FIELD_MISSING` (keep the stored report). In
  `storage.py`, `body.read()` sits outside the error mapping, so a stream timeout is
  `INTERNAL_ERROR`, not a retryable `TIMEOUT`, and `put()` maps nothing. DB backstop:
  `variant_matrix_complete` does not fire when a child's `parent_product_id` goes NOT NULL → NULL
  (the import runner checks it; a DB-level check needs its own migration).
- **Image approve gate hand-off to S19 (S17b T14b; blocks DW3 at the endpoint and AC17).** S17b
  ships the gate as the pure `app.services.image_gate.evaluate_image_gate(listing, stored,
  pipeline_reports)` and `ImageRepository.live_reports_for_images(image_ids)` (one SELECT per
  batch). S19's `approve_batch` must call it inside each item's savepoint, after
  `BULK_APPROVE_REQUIRES_VALIDATED` and before pins and maker-checker: `stored` = each image's
  report for `listing.marketplace`, `pipeline_reports` = the version's `image_reports_json`; any
  `blocking` item → item 422 `IMAGE_NON_COMPLIANT` with an `ErrorReport` of blocking then warning
  items, savepoint rolled back, no decision row. An unchecked (pending, deleted or foreign) image
  blocks; vision failures only warn (C8). The T14b tests, the `api-tests-approve-image-gate`
  prove leg and the e2e approve leg (`scripts/e2e/image_pipeline.sh`) move to S19.
- **Publish re-hash of image objects (S21; S17b PLAN D9).** The signed PUT is create-only
  (`If-None-Match: *` is signed) and the job stores `content_hash` = sha256 of the bytes it
  checked. S21's publish must re-read each image, compare its sha256 with `content_hash` and fail
  closed on a mismatch or a NULL hash, so a report never vouches for other bytes. GCS S3-interop
  support for `If-None-Match: *` on a signed PUT is not verified (S4).
- **Image check identity boundary (S4; S17b CP6).** The image check child gets an env allow-list
  (no DSN, token, key or secret) but runs as the worker's uid on the same network: a decoder
  code-execution bug could still read `/proc/<ppid>/environ` or ask the metadata server for the
  worker service-account token. The v1 controls are S17a's JPEG/PNG/TIFF-only decoders and
  pip-audit on Pillow. Fix at S4: a least-privilege worker service account, and the image check
  under a separate unprivileged identity or service with no KMS or DB access. `RLIMIT_CPU`/`AS`
  apply on Linux only.
- **Image-pipeline follow-ups from the S17b build (LOW).** The reaper re-defers a reaped
  `image_compliance` job without its `image_compliance` queue lock (`JobRunRepository.reap`), so a
  reaped image job can run beside another (the per-process semaphore still holds one per worker);
  fix: derive the lock from the kind on every defer. A dead-lettered or failed image job leaves
  the row `pending` (the gate blocks it); only a new upload or a staff requeue recovers it, and a
  re-registration of the same key replays the dead job. The text overlay, watermark and logo rules
  are not seeded (T119: `ImageRequirementParams` needs a parameter they lack).
