# Launch hardening record (S26, v1)

The local launch audit for v1 (S26 AC 13, DW5; PLAN D10). One row per check: the test that
proves it, the command that runs it, the date of the fresh output it was last seen green with, the
severity it would carry if it failed, and its status. Production-only checks are deferred to the
step that can run them; named skips and known gaps have their own rows.

**Open P0/P1: 0**

Severity (PLAN D10): **P0** cross-tenant access, auth bypass, credential leak, publish without
approval; **P1** maker-checker bypass, data loss, duplicate marketplace write, unbounded resource
use (P0 and P1 block launch); **P2** a wrong answer or state with no data or security impact;
**P3** cosmetic or tooling. Status: `pass`, `deferred` (to the step named), `skip` (a named
skip), `waived` (a waiver row elsewhere), `known gap` (recorded, owned, not launch-blocking).

Test ids: `path::test` is a pytest test, `path#leg` an acceptance leg or e2e step, `make <t>` a
make target. Commands: `AT` = `make api-tests api-tests-integration` (the two required shards);
`ISO` = `make tenant-isolation`.

## Checks

| check | test id | command | fresh output | severity | status |
|---|---|---|---|---|---|
| Surface complete: no stub op, every op has its case matrix (AC 1) | `apps/api/tests/security/test_surface_complete.py::test_v1_surface_has_no_stubs`; `apps/api/tests/unit/test_case_matrix_guard.py::test_implemented_routes_have_case_matrix` | AT | 2026-10-10 | P1 | pass |
| RBAC: every denied role on every op is 403 (API5) | `apps/api/tests/security/test_owasp_api5_api6.py::test_api5_denied_role_403` | AT | 2026-10-10 | P0 | pass |
| RBAC: switch to a non-member or inactive tenant is 404 | `apps/api/tests/security/test_owasp_api2_auth.py::test_api2_switch_tenant_non_member_or_inactive_404` | AT | 2026-10-10 | P0 | pass |
| RBAC: a pre-switch access token stays scoped to its old tenant | `apps/api/tests/security/test_owasp_api2_auth.py::test_api2_pre_switch_access_token_still_scoped_to_old_tid` | AT | 2026-10-10 | P0 | pass |
| RBAC: after a switch the role is the target membership's | `apps/api/tests/security/test_owasp_api5_api6.py::test_api5_switch_role_is_target_membership_role` | AT | 2026-10-10 | P0 | pass |
| RBAC: a demoted reviewer's live token cannot approve | `apps/api/tests/security/test_owasp_api5_api6.py::test_api5_demoted_reviewer_live_token_cannot_approve` | AT | 2026-10-10 | P1 | pass |
| RBAC over HTTP: switch and members flows | `scripts/e2e/auth_session.sh#switch_tenant_200`; `scripts/e2e/members.sh#member_patch_403` | make e2e-fresh | 2026-10-10 | P0 | pass |
| Cross-tenant: a foreign id is 404 on every op (API1) | `apps/api/tests/security/test_owasp_api1_bola.py::test_api1_foreign_id_404` | AT | 2026-10-10 | P0 | pass |
| Cross-tenant: lists hold 0 foreign rows, every page | `apps/api/tests/security/test_owasp_api1_bola.py::test_api1_lists_hold_no_foreign_rows` | AT | 2026-10-10 | P0 | pass |
| Cross-tenant: RLS as app_rw (raw SQL) | `apps/api/tests/tenant_isolation/test_rls_raw_sql.py::test_rls_raw_sql_cross_tenant`; `apps/api/tests/tenant_isolation/test_app_rw.py::test_isolation_runs_as_app_rw` | ISO | 2026-10-10 | P0 | pass |
| Maker-checker: the generator's approve is refused before SQL (API6) | `apps/api/tests/security/test_owasp_api5_api6.py::test_api6_generator_approve_403_maker_checker` | AT | 2026-10-10 | P1 | pass |
| Maker-checker: the DB backstop refuses self-approval (`MKC01`) | `apps/api/tests/security/test_owasp_api5_api6.py::test_api6_db_backstop_refuses_self_approval` | AT | 2026-10-10 | P1 | pass |
| Maker-checker over HTTP: owner item 403, operator 403, reviewer 200 | `scripts/e2e/acceptance.sh#owner_self_approve_item_403`; `scripts/e2e/acceptance.sh#operator_approve_permission_denied`; `scripts/e2e/acceptance.sh#reviewer_approve_200` | make acceptance | 2026-10-10 | P1 | pass |
| Publish needs an approved, current version (invariant 1) | `apps/api/tests/security/test_owasp_api5_api6.py::test_api6_publish_not_approved_refused`; `apps/api/tests/security/test_owasp_api5_api6.py::test_api6_publish_stale_version_refused` | AT | 2026-10-10 | P0 | pass |
| KMS: token_enc holds no canary bytes and opens only via the adapter path | `apps/api/tests/security/test_secrets_never_logged.py::test_token_enc_has_no_canary_bytes`; `apps/api/tests/security/test_secrets_never_logged.py::test_token_enc_decrypts_only_via_adapter_path` | AT | 2026-10-10 | P0 | pass |
| KMS: a restored token_enc decrypts with the local key | `scripts/drill/check_restore.py#token_enc_decrypts` | make drill-restore | 2026-10-10 | P1 | pass |
| KMS in staging/prod: Cloud KMS wrapper | - | S4 (the wrapper is built and unit-tested against a fake KMS; the live round trip needs the staging key) | - | P0 | deferred |
| CORS: a disallowed origin gets no ACAO; no wildcard (API8) | `apps/api/tests/security/test_owasp_api8_config.py::test_api8_disallowed_origin_no_acao`; `apps/api/tests/security/test_owasp_api8_config.py::test_api8_no_wildcard_origin` | AT | 2026-10-10 | P1 | pass |
| Security headers on every response | `apps/api/tests/unit/test_security_headers.py::test_security_headers_set_on_every_response` | AT | 2026-10-10 | P2 | pass |
| TLS audit (R06718d) | - | S4 staging (TLS terminates at the load balancer) | - | P1 | deferred |
| Secrets never in logs, Sentry, spans, DB sinks, responses (AC 3) | `apps/api/tests/security/test_secrets_never_logged.py::test_secrets_never_logged_in_logs`; `apps/api/tests/security/test_secrets_never_logged.py::test_secrets_never_logged_in_sentry_events`; `apps/api/tests/security/test_secrets_never_logged.py::test_secrets_never_logged_in_spans`; `apps/api/tests/security/test_secrets_never_logged.py::test_secrets_never_logged_in_db_sinks`; `apps/api/tests/security/test_secrets_never_logged.py::test_secrets_never_logged_in_api_responses` | AT | 2026-10-10 | P0 | pass |
| Secrets over the acceptance run: no password, JWT or refresh token in the stack's logs | `scripts/e2e/acceptance.sh#no_canaries_in_logs` | make acceptance | 2026-10-10 | P0 | pass |
| Credentials never in a GenerationContext (invariant 6) | `apps/api/tests/security/test_secrets_never_logged.py::test_every_pushed_generation_context_passes_seam_walk` | AT | 2026-10-10 | P0 | pass |
| Full-history gitleaks | `make gitleaks` | nightly `gitleaks-history` | 2026-10-10 | P0 | pass |
| API2 broken authentication | `apps/api/tests/security/test_owasp_api2_auth.py::test_api2_bad_tokens_401`; `apps/api/tests/security/test_owasp_api2_auth.py::test_api2_refresh_reuse_revokes_family`; `apps/api/tests/security/test_owasp_api2_auth.py::test_api2_sixth_login_429` | AT | 2026-10-10 | P0 | pass |
| API3 property-level authorisation | `apps/api/tests/security/test_owasp_api3_properties.py::test_api3_unknown_fields_422`; `apps/api/tests/security/test_owasp_api3_properties.py::test_api3_no_response_holds_denied_key` | AT | 2026-10-10 | P0 | pass |
| API4 resource consumption (body cap, limits, Retry-After) | `apps/api/tests/security/test_owasp_api4_resources.py::test_api4_body_over_cap_413`; `apps/api/tests/security/test_owasp_api4_resources.py::test_api4_121st_tenant_request_429`; `apps/api/tests/security/test_owasp_api4_resources.py::test_api4_csv_10001_rows_refused` | AT | 2026-10-10 | P1 | pass |
| API5 function level: internal admin 404 in production | `apps/api/tests/security/test_owasp_api5_api6.py::test_api5_internal_admin_404_in_production` | AT | 2026-10-10 | P0 | pass |
| API6 business flows: budget 429, submission cap | `apps/api/tests/security/test_owasp_api5_api6.py::test_api6_budget_429`; `apps/api/tests/security/test_owasp_api5_api6.py::test_api6_retry_fields_after_cap_refused` | AT | 2026-10-10 | P1 | pass |
| API7 SSRF: URLs open no socket; outbound hosts from settings | `apps/api/tests/security/test_owasp_api7_ssrf.py::test_api7_urls_in_csv_open_no_socket`; `apps/api/tests/security/test_owasp_api7_ssrf.py::test_api7_outbound_hosts_only_from_settings`; `apps/api/tests/security/test_owasp_api7_ssrf.py::test_api7_follow_redirects_never_true` | AT | 2026-10-10 | P0 | pass |
| API8 misconfiguration: 500 envelope, docs off in production | `apps/api/tests/security/test_owasp_api8_config.py::test_api8_forced_500_envelope`; `apps/api/tests/security/test_owasp_api8_config.py::test_api8_docs_and_openapi_404_in_production` | AT | 2026-10-10 | P1 | pass |
| API9 inventory: routes == OpenAPI == registry | `apps/api/tests/security/test_owasp_api9_api10.py::test_api9_mounted_routes_equal_openapi_equal_registry`; `apps/api/tests/security/test_owasp_api9_api10.py::test_api9_committed_openapi_matches_app` | AT | 2026-10-10 | P2 | pass |
| API10 unsafe consumption: marketplace, OAuth and pipeline replies | `apps/api/tests/security/test_owasp_api9_api10.py::test_api10_marketplace_reply_errorreport`; `apps/api/tests/security/test_owasp_api9_api10.py::test_api10_oauth_reply_errorreport`; `apps/api/tests/security/test_owasp_api9_api10.py::test_api10_pipeline_invalid_result_rejected` | AT | 2026-10-10 | P1 | pass |
| Live Schemathesis, four roles, stateful (AC 4; two checks excluded, approved 2026-10-10) | `make fuzz` | make fuzz (seeds 2630, 2631) | 2026-10-10 | P1 | pass |
| Prompt injection: the rules outcome equals a neutral string's (AC 6) | `apps/api/tests/security/test_prompt_injection.py::test_prompt_injection_rules_outcome_equals_neutral`; `apps/api/tests/security/test_prompt_injection.py::test_prompt_injection_csv_import`; `apps/api/tests/security/test_prompt_injection.py::test_prompt_injection_content_patch` | AT | 2026-10-10 | P1 | pass |
| Fault: 429 x n, backoff, success in the limiter | `apps/api/tests/integration/test_failure_injection_marketplace.py::test_failure_injection_429_backoff_then_success` | AT | 2026-10-10 | P1 | pass |
| Fault: token expired mid-publish, one refresh | `apps/api/tests/integration/test_failure_injection_marketplace.py::test_failure_injection_token_expired_mid_publish_one_refresh` | AT | 2026-10-10 | P1 | pass |
| Fault: `invalid_grant` -> AUTH_EXPIRED + reconnect task | `apps/api/tests/integration/test_failure_injection_marketplace.py::test_failure_injection_invalid_grant_auth_expired_reconnect_task` | AT | 2026-10-10 | P2 | pass |
| Fault: 2 of 5 fields rejected -> partial | `apps/api/tests/integration/test_failure_injection_marketplace.py::test_failure_injection_partial_two_of_five_fields` | AT | 2026-10-10 | P2 | pass |
| Fault: connection dropped after the write -> pending, read before retry, one write | `apps/api/tests/integration/test_failure_injection_marketplace.py::test_failure_injection_drop_after_write_pending_read_before_retry` | AT | 2026-10-10 | P1 | pass |
| Fault: 5xx after the write -> unconfirmed, no second write | `apps/api/tests/integration/test_failure_injection_marketplace.py::test_failure_injection_5xx_after_write_unconfirmed` | AT | 2026-10-10 | P1 | pass |
| Fault: a retry resends only the failed fields (invariant 10) | `apps/api/tests/integration/test_failure_injection_marketplace.py::test_failure_injection_retry_resends_only_failed_fields`; `scripts/e2e/acceptance.sh#retry_fields` | AT; make acceptance | 2026-10-10 | P1 | pass |
| Fault: same Idempotency-Key twice -> one write | `apps/api/tests/integration/test_failure_injection_jobs.py::test_failure_injection_duplicate_publish_same_key_one_write` | AT | 2026-10-10 | P1 | pass |
| Fault: two publish jobs on one listing -> one proceeds | `apps/api/tests/integration/test_failure_injection_jobs.py::test_failure_injection_two_jobs_one_listing_one_proceeds` | AT | 2026-10-10 | P1 | pass |
| Fault: worker killed mid-publish -> reaper, one write | `apps/api/tests/integration/test_failure_injection_jobs.py::test_failure_injection_worker_kill9_reaper_one_write` | AT | 2026-10-10 | P1 | pass |
| Fault: pipeline timeout / malformed result -> JOB_TIMEOUT / INTERNAL_ERROR, no version | `apps/api/tests/integration/test_failure_injection_jobs.py::test_failure_injection_stub_timeout_job_timeout_no_version`; `apps/api/tests/integration/test_failure_injection_jobs.py::test_failure_injection_stub_malformed_internal_error` | AT | 2026-10-10 | P2 | pass |
| Fault: corrupt image -> CORRUPT; approve refused (IMAGE_NON_COMPLIANT) | `apps/api/tests/integration/test_failure_injection_jobs.py::test_failure_injection_corrupt_image_corrupt` | AT | 2026-10-10 | P2 | pass |
| Fault: DB backend terminated mid-job (3-attempt kind) -> retried, no duplicates | `apps/api/tests/integration/test_failure_injection_jobs.py::test_failure_injection_pg_terminate_backend_three_attempt_kind_retried_no_duplicate_rows` | AT | 2026-10-10 | P1 | pass |
| Fault: DB backend terminated mid-generation -> failed once, reservation kept | `apps/api/tests/integration/test_failure_injection_jobs.py::test_failure_injection_pg_terminate_backend_generation_fails_once` | AT | 2026-10-10 | P2 | pass |
| Known gap, DATABASE open #19: per-call `agent_runs` lost on a crash or timeout (invariant 7) | `apps/api/tests/integration/test_failure_injection_jobs.py::test_failure_injection_pg_terminate_backend_generation_fails_once` | spend bounded by `cost_reserved_usd`; AI-team callback proposed, TEAM-QUESTIONS T131 | 2026-10-10 | P2 | known gap |
| Restore drill: pg_dump -> throwaway container (0000 + passwords) -> pg_restore, every check, <= 15 min (AC 8) | `make drill-restore`; `apps/api/tests/drill/test_pg_dump_restore_round_trip.py::test_pg_dump_restore_round_trip` | make drill-restore (5 s); pytest -m drill | 2026-10-10 | P1 | pass |
| Restore drill in Cloud SQL (PITR, non-superuser restore) | - | S25 DW6, TEAM-QUESTIONS T87 | - | P1 | deferred |
| Load smoke: 1,000-row CSV, 20 concurrent generations, bharat not starved (AC 7) | `make load-smoke` | make load-smoke (numbers below) | 2026-10-10 | P1 | pass |
| Staging load and walkthrough (DW7) | - | after S4 and billing (DW7) | - | P1 | deferred |
| v1 acceptance run, setup -> analytics, twice on fresh stacks (AC 9) | `make acceptance`; `scripts/e2e/acceptance.sh#analytics` | make acceptance (x2) | 2026-10-10 | P1 | pass |
| Acceptance: a live listing has its marketplace id and URL | `scripts/e2e/acceptance.sh#status_sync`; `apps/api/tests/integration/test_status_sync_live_url.py::test_status_sync_live_stores_the_live_url` | make acceptance; AT | 2026-10-10 | P2 | pass |
| Acceptance: a family's normalized colour reaches the marketplace | `scripts/e2e/acceptance.sh#publish`; `apps/api/tests/integration/test_adapter_input_normalized_colour.py::test_family_child_colour_is_the_normalized_colour` | make acceptance; AT | 2026-10-10 | P2 | pass |
| Acceptance: Flipkart publish refused while the flag is off | `scripts/e2e/acceptance.sh#flipkart_publish_refused` | make acceptance | 2026-10-10 | P0 | pass |
| Every flow has an HTTP-only e2e script, twice on a fresh stack (AC 10) | `apps/api/tests/meta/test_e2e_inventory.py::test_every_flow_has_e2e_script`; `apps/api/tests/meta/test_e2e_inventory.py::test_e2e_scripts_http_only`; `make e2e-fresh` | make e2e-fresh | 2026-10-10 | P2 | pass |
| Mutation: each core module >= 80% or a waiver (AC 11, DW6) | `make mutation`; `apps/api/tests/unit/test_mutation_report.py::test_mutation_report_below_80_fails` | make mutation: partial local run (1,016 of 3,832 mutants), the full run is the nightly `mutation` leg (S26 AC 11); see Mutation scores | 2026-10-10 | P3 | deferred |
| Nightly: schedule + dispatch, SHA-pinned, no secret but the report's token (AC 12) | `apps/api/tests/meta/test_workflows.py::test_nightly_triggers_schedule_and_dispatch_only`; `apps/api/tests/meta/test_workflows.py::test_nightly_only_report_job_has_issues_write` | AT; first manual run after merge on the user's go | 2026-10-10 | P3 | pass |
| Production smoke and production OAuth (DW8) | - | S25 (TEAM-QUESTIONS T77, T78) | - | P1 | deferred |
| Real SP-API / Flipkart calls and marketplace credentials | - | S20/S22 DW1 | - | P1 | deferred |
| Sentry / Cloud Trace exporters | - | S4 / S24 | - | P3 | deferred |
| UAT with 2 client people | - | TEAM-QUESTIONS T8 | - | P2 | deferred |
| Runbooks, incident response, on-call, training (R13671, R13673-4) | - | TEAM-QUESTIONS T81 | - | P2 | deferred |
| The client's 50-image library (R06694b) | - | client hand-off, TEAM-QUESTIONS T8 | - | P3 | deferred |

## Named skips

| named skip | leg | trigger |
|---|---|---|
| C2 | `scripts/e2e/acceptance.sh#ask_question_skip` | The BRD's "ask one targeted question" for a missing marketplace fact is not in v1 (VISION C2): the leg prints `SKIP C2` and the run goes on; it becomes a real leg when the question flow ships. |
| T41 | `scripts/e2e/acceptance.sh#remap_skip` | The BRD's adapter remap of a rejected size and an automatic resubmit (DATABASE.md:1798, L2129-2135) is T41, out of scope in S20/S21 (`fix_action` is never executed); the run proves the transient-resend path instead (PLAN D14, note under TEAM-QUESTIONS T8). |

## Deferred items

| deferred item | to | why |
|---|---|---|
| Staging load and walkthrough | DW7, after S4 + billing | needs a staging stack |
| Production smoke, production OAuth | S25 (T77, T78) | needs production |
| TLS audit (R06718d) | S4 staging | TLS ends at the load balancer |
| Cloud SQL PITR, non-superuser restore | S25 DW6 (T87) | needs Cloud SQL |
| Cloud KMS live round trip | S4 | the wrapper is unit-tested against a fake KMS; no staging key yet |
| Real marketplace calls and credentials | S20/S22 DW1 | needs seller accounts |
| Nightly's first scheduled run | after merge, on the user's go (S26 AC 12) | needs the workflow on main |

## Load smoke numbers (make load-smoke, 2026-10-10, final run)

```json
{
  "phases": {
    "bharat": {"requests": 17, "p50_ms": 19.5, "p95_ms": 59.9},
    "generate": {"requests": 41, "p50_ms": 21.3, "p95_ms": 215.1},
    "import": {"requests": 13, "p50_ms": 17.6, "p95_ms": 118.4},
    "login": {"requests": 4, "p50_ms": 51.1, "p95_ms": 73.3}
  },
  "counts": {
    "import_imported": 1000, "import_failed": 0, "import_seconds": 27.3,
    "generations_succeeded": 20, "generate_seconds": 3.2, "dead_letters": 0,
    "server_errors": 0, "rate_limited": 0,
    "bharat_generation_start_s": 3.1, "bharat_publish_leaves_queued_s": 0.0,
    "tenant_requests_peak_per_min": {"acme": 55, "bharat": 20}
  }
}
```

The first run the same day (load smoke T18): 1,000/1,000 rows in 40.0 s, 20/20 generations in
3.4 s, bharat start 3.2 s, peak acme 59 requests a minute; generate p95 332.8 ms.

## Mutation scores (make mutation, 2026-10-10, partial)

The local run generated 3,832 mutants over the nine core modules and was stopped after 48 minutes
with 1,016 checked (the integration-backed mutants run one child at a time, ~5-15 a minute), so it
is not a score yet: `scripts/ci/mutation_report.py` fails every module with an unchecked mutant.
The full run is the nightly `mutation` leg (360-minute budget). Detected = killed + timeout +
caught by the type check; counted = every checked mutant but the skipped ones ("no tests"
counts against the module).

| module | detected / counted (checked so far) | not checked | partial score |
|---|---|---|---|
| app/services/rules_engine | 45 / 98 | 1,947 | 45.9% |
| app/db/guard.py | 0 / 0 | 110 | - |
| app/repositories/base.py | 31 / 74 | 233 | 41.9% |
| app/services/maker_checker.py | 0 / 0 | 32 | - |
| app/services/idempotency.py | 3 / 6 | 49 | 50.0% |
| app/repositories/idempotency.py | 39 / 63 | 75 | 61.9% |
| app/services/publish_rules.py | 94 / 105 | 19 | 89.5% |
| app/services/publish_ledger.py | 243 / 291 | 0 | 83.5% (complete) |
| app/services/publish.py | 155 / 379 | 351 | 40.9% |

Several partial scores are under 80% (much of it "no tests": functions the selected test files
never reach). A module that ends under 80% needs more tests or a row in `MUTATION-WAIVERS.md`,
which only the user can approve; none is written yet.
