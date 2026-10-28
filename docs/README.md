# Documentation index

All project documents are kept in this folder. For large files, run `grep -n '^#' <file>` to list the
sections, then read only the one you need.

**When sources disagree**, the first in this list wins:
1. the Tech Lead's latest decision, recorded in `STATE.md`
2. the client spec site (`source/`)
3. team-confirmed answers in `review/TEAM-QUESTIONS.md`
4. accepted ADRs (`adr/`)
5. `canon/CANON-ADDENDUM.md`
6. `canon/CANON.md`
7. `canon/ENV.md`
8. `drafts/`
9. `../scaffold/`
10. the front-door summaries (`PRD.md`, `ARCH.md`, `RULES.md`, `DESIGN.md`, `PHASES.md`, `PROCESS.md`): they never
    override a source; on conflict, the highest-ranked source above wins and the summary is fixed in
    the same PR

Not ranked yet: `architecture/`, the body of `EXECUTION-PLAN.md` and the proposed ADRs 0006–0011.
None of them overrides ranks 1–7 on its own: `EXECUTION-PLAN.md` overrides nothing until the user
approves the matching §2 item (its "Status" line); `DATABASE.md` rev 3 is the schema source (STATE
open decision 10, accepted 2026-10-04); the Canon row for ADRs 0006–0008 is "proposed, not yet in
force" (`canon/CANON.md` "Amendments by ADR"). Their rank against each other and against `drafts/` is
Open: STATE pending decision P15.

## Start here

The first seven files are the front door, read in this order. The first six are short summaries that
point into the detailed documents below, which stay the single source of truth. `RULES.md` is the
engineering rulebook; it wins over the local agent rules file, and the sources it cites win over it
(`RULES.md:5-7`). `PROCESS.md` is the tracked team process; the local agent rules add only
editor-specific detail.

| File | Size | What it is | Read when |
|---|---:|---|---|
| `PRD.md` | 12 KB | What v1 must do, for whom, and what it leaves out | First, when joining |
| `ARCH.md` | 12 KB | How the backend is built and where it runs | When joining; before structural changes |
| `RULES.md` | 12 KB | The engineering rules every contributor follows, and how the docs stay current | Before any change |
| `DESIGN.md` | 11 KB | Backend design conventions: layers, API, data, jobs, listing lifecycle, adapters, the AI seam, observability | Before designing an endpoint, table or job |
| `HANDOFF-AI.md` | 6 KB | The generation seam the AI team implements: `run_listing_pipeline`, `PipelineDeps`, the context and result rules | When the AI team builds or changes the pipeline |
| `HANDOFF-FE.md` | 5 KB | The v1 API for the FE team: installing `@mkos/schemas`, the mock, the conventions, the screens and their operations | When the FE team starts or a contract changes |
| `EXTERNAL-SETUP.md` | 8 KB | What only the client can do: Amazon and Flipkart developer applications, the domain and website, GCP billing, keys | Before staging and before connecting real stores |
| `PHASES.md` | 12 KB | Phases and build status, from setup to launch | When picking or planning work |
| `PROCESS.md` | 15 KB | The team process: roles, the team loop, approvals, the session protocol, the git flow, local-only files, running the loop by hand | Every session; before planning or building a step |
| `STATE.md` | 44 KB | Live status, decisions, pending user decisions, next actions, the handoff log | Every session |
| `STEPS.md` | 70 KB | The v1 build steps S2–S26: scope, dependencies, testable "Done when" lines, stand-ins, waits (approved 2026-10-04) | Before planning any step |
| `EXECUTION-PLAN.md` | 67 KB | The plan from setup to launch: decisions, Phase 0 setup, GitHub + CI/CD, GCP runbook, roadmap S1–S26, third-party agent add-on verdict | Before setup work or planning a step |

## Folders

| Folder | Authority | Contents | Read when |
|---|---|---|---|
| `source/` | **2: the source of truth** (rank 1 is the Tech Lead's decisions in `STATE.md`) | The client spec site snapshot: `system-arch-llms-full.txt` (388 KB, 244 pages, `^# ` = page), `system-arch-content.json` (page metadata, `scope`), `system-arch-llms.txt`. Never edited; `../scripts/spec-sync.sh` checks it against the live site | To check what the client actually said (cite `L<line>`) |
| `adr/` | 4: accepted ADRs win over the Canon; proposed ones are not ranked yet (P15) | Architecture decisions. 0001–0005, 0012 and 0013 accepted; 0006–0011 ⚑ proposed (built as written until the team rules). `adr/README.md` is the ledger | Before changing stack, hosting, identity, release or test rules |
| `canon/` | 5–7: engineering rulings | `CANON.md` (52 KB: naming, migrations and env register; its top "Amendments by ADR" section wins), `CANON-ADDENDUM.md` (41 KB, closing rulings; wins over CANON), `ENV.md` (12 KB, every env var) | For any name, path, env var, migration id or code |
| `architecture/` | Design (our proposal, site-aligned); not ranked yet (P15) | `DATABASE.md` (244 KB: every table, column, constraint, RLS, roles, migration ledger §8.2, lifecycles §6, flows §7, open decisions §11.1, site mapping Appendix A) and `BACKEND-ARCHITECTURE.md` (248 KB: journeys §3, internals §4, API §5, AI seam §6, jobs §8, security §9, GCP §10, delivery §11). Sources of the Word docs | While planning or building any step |
| `handover/` | Handover for the frontend and AI teams, checked against the code | `README.md` (start here), `API-REFERENCE.md` and `swagger.html` (generated from the contract), `ARCHITECTURE.md`, `DATABASE.md` (ER diagrams; schema part generated), `FLOWS.md` (sequence diagrams and state machines), `FRONTEND-GUIDE.md`, `AI-TEAM-GUIDE.md`. Regenerate with `make docs-handover` | Before integrating with the API or the generation seam |
| `review/` | Review outputs; questions for the team | `TEAM-QUESTIONS.md` (T1–T132; each with the default we build), `REQUIREMENTS-TRACE.md` (215 KB, generated: every spec statement's verdict), `SPEC-TRACEABILITY.md` (page by page), `VISION-ALIGNMENT.md` (G/C/Q ids), `KNOWN-ISSUES.md` and `SUPERSEDED.md` (fixes that override the drafts and scaffold), `LAUNCH-HARDENING.md` (S26: every launch check with its test id, fresh output, severity and status; 0 open P0/P1) and `MUTATION-WAIVERS.md` (S26: the mutation gate's waiver rows, read by `scripts/ci/mutation_report.py`) | Before planning a step: pull every row that touches it into PLAN.md |
| `trace/` | Data behind REQUIREMENTS-TRACE | `cNN.json` (every spec line → R-id, 23 chunks, 100% of lines) and `cNN.map.json` (a verdict per statement), `_chunks.json`. Query with python or jq; never read whole (3.9 MB) | When a verdict changes; then run `../scripts/trace/check_mapping.py` and `compile_trace.py` |
| `drafts/` | 8: pre-Canon, intent only | `BUILD-SEQUENCE.md` (45 KB, build steps S1–S14; its S2–S14 "Done when" lines are replaced by `../STEPS.md`), `CONTRACTS.md` (53 KB), `CHECKLISTS.md` (183 KB, grep only), `ARCHITECTURE.md`, `DECISIONS.md`, `BLOCKERS.md`, `DELEGATE-GATE.md`. Never copy a value without checking ranks 1–7 | For a step's scope and "Done when"; for intent behind a rule |
| `research/` | Background | `stack-2026-09.md`, `hosting-2026-09.md`, `catalogus.md` (competitor) | When revisiting a stack or hosting choice |
| `plans/` | Loop state | `README.md` (loop rules and file formats; the only committed file here). Per step, `docs/plans/<id>/` holds CONTEXT, PLAN, CODE-PLAN, LOOP, DEVIATIONS, `repro/` and REPORT: **local-only** (`.git/info/exclude`), never committed; the PR description carries the scope, criteria and results | During each build step |
| `runbooks/` | Operations, below the Canon and ADRs | `DEPLOY-GCP.md`: the by-hand GCP deploy toolkit (`../scripts/deploy/gcp/deploy.sh`), the first-deploy order, rollback, PITR restore, costs and known limits. `MFA-STAFF.md` (DPP-1, ADR-0021): the identity check, `unlock` and `reset-mfa`, and the order for switching MFA on. `EDGE-CUTOVER.md`: the prod API edge (load balancer, Cloud Armor, locked ingress) cutover with a rollback per step (DPP-2, ADR-0022) | Before deploying to staging or prod; before unlocking an account or resetting MFA |
| `archive/` | None | Retired files (the v0 rules file) and `STATE-log.md` (older STATE session-log entries), frozen. Do not follow | Never, unless asked |

**Outside `docs/`:**
- `../deliverables/` holds only the team Word docs, generated by `../scripts/docgen/build.sh`. Edit the
  `.md`, then rebuild.
- `../scaffold/` holds reference starter files with known bugs. Later steps take files one by one,
  after the SUPERSEDED/KNOWN-ISSUES fixes; never `apps/web` or `packages/pipeline` (ADR-0012).
- `../packages/pipeline-stub/` is the in-repo stub of the AI pipeline (S14b); `../scripts/e2e/` holds the
  local HTTP e2e scripts (`make e2e`; `make e2e-onboarding` runs `../ONBOARDING.md` on a clean clone).
- `../ONBOARDING.md` is the newcomer's guide: the invariants, the seam ownership line, the repo tour and
  every command to run (each checked by a meta test).

## Moved on 2026-09-29 (old → new)

Any older reference to `docs/context/…` resolves like this. File names are unique, so a bare name
such as `CANON.md:58` still means the same file.

| Old | New |
|---|---|
| `docs/context/CANON.md`, `CANON-ADDENDUM.md`, `ENV.md` | `docs/canon/` |
| `docs/context/BUILD-SEQUENCE.md`, `ARCHITECTURE.md`, `CONTRACTS.md`, `DECISIONS.md`, `CHECKLISTS.md`, `BLOCKERS.md`, `DELEGATE-GATE.md` | `docs/drafts/` |
| `docs/context/TEAM-QUESTIONS.md`, `REQUIREMENTS-TRACE.md`, `SPEC-TRACEABILITY.md`, `VISION-ALIGNMENT.md`, `KNOWN-ISSUES.md`, `SUPERSEDED.md` | `docs/review/` |
| `docs/context/req-trace/` | `docs/trace/` |
| `deliverables/.docgen-cache/` | `scripts/docgen/.cache/` |
