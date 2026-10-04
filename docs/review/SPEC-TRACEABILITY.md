# SPEC TRACEABILITY: every client spec page against our plan

_Written 2026-09-27 (Day 7). **Status: traceability record.** It changes no rule. Every change it lists
needs the user's OK, an ADR or a client answer (`docs/PROCESS.md` §2, §5)._

## Are we aligned with every page?

**No, not yet.** Of the site's 244 pages, 71 are covered: 37 as written and 34 through a deliberate ADR
or Canon change that we only need to tell the client about. Another 33 belong to another team (8) or
are Phase 2/3 or future (25). The remaining 140 need action: 111 gaps, 10 conflicts and 19 client questions.
The build plan and the specs match our principles: the same invariants, safety chain, tenancy,
ErrorReport, maker-checker, adapters and non-goals. Most of the gaps are schema or contract shape. They
cluster around attribute provenance, compliance fields, the publish ledger and LIVE status, review
decisions, issues and human tasks, rule types (image rules included), versioned templates, variants,
batch contracts, image verdicts per marketplace, brand chunks, outbound rate limits, model escalation
and the order of work after S14. They are cheap to fix before migrations 0001-0003 and the
`schemas-v1.0.0` tag, and a MAJOR three-team change after. All 11 Founder BRD pages are a gap or a
conflict, because the Canon never read the BRD. The last section lists every required change in order.

**Method.** The source is `docs/source/system-arch-llms-full.txt`; `L<n>` is a line in that file. 175
pages carry verdicts from the audit behind `VISION-ALIGNMENT.md`; the other 69 were assessed on
2026-09-27. Our direction is AGENTS.md, STATE.md "Decided", CANON.md with its ADR amendments, the
Addendum, ADR-0001 to ADR-0005, DATABASE.md (a draft) and catalogus.md. A page takes its most severe
verdict (conflict > gap > needs-client > aligned-changed > aligned). It is other-team or later-phase
only when the whole page is. The audit's "superseded-by-canon" counts as aligned-changed. Verdicts
written before ADR-0001 to ADR-0005 (Redis, ARQ, asyncpg, AWS, PG16) were re-checked against the page
text. G, C and Q IDs are VISION-ALIGNMENT's; "new" marks a finding it lacks.

**Verification (skeptic A, 2026-09-27).** All 89 rows then marked aligned or aligned-changed were
re-read against their pages (the Founder BRD had none). 30 fell to gap or needs-client under the
most-severe rule: many already named a gap in their own note, or sat in the list below. Two conflicts
([87], [88]) no longer held, because DATABASE.md now keeps `tenants.guidelines` as JSONB.

**Verification (skeptic B, 2026-09-27).** The 112 build-plan and spec rows the first pass marked gap,
conflict or needs-client were re-read against their pages and our direction, including ADR-0006 to
ADR-0011 (accepted later on 2026-09-27) and the TEAM-QUESTIONS defaults. As for skeptic A, an item
DATABASE.md only tags Proposed doesn't close a gap. 12 rows fell to aligned, aligned-changed or
other-team: P05, [15], [48], [50], [68], [99], [100], [123], [125], [127], [161], [187]. 8 moved
between action statuses: P01, P12, [25], [71] and [132] became gaps (P12's real issue is the missing
reject contract, not the year-2 ceiling), and [1], [14] and [96] became client questions, because the
site contradicts itself on performance data (TEAM-QUESTIONS T14). 28 other notes were corrected, for
example: ImageReports are already kept per marketplace on each listing version, and ADR-0008 to
ADR-0010 now cover approval records, rollback and UAT. The list below was updated to match; every
action page appears in it.

### Legend

| Status | Meaning |
|---|---|
| ✅ aligned | Our direction covers it as written |
| 🔁 aligned-changed | Covered, but deliberately changed by an ADR or Canon row, which the Refs column names |
| ⚠️ gap | Required, but not yet planned |
| ❌ conflict | Our direction contradicts it and nothing has ruled on it yet |
| ❓ needs-client | Ambiguous, or the client's choice to make |
| 🧩 other-team | The AI team (`packages/pipeline`) or the FE team (`apps/web`) owns it; the note says what it means for our seam |
| ⏭ later-phase | Phase 2, Phase 3 or future in the source |

**Owners:** BE = us (backend + DB); AI = the AI team; FE = the FE team; shared = both our seam and
another team; client = a client decision; ops = infrastructure and cost (our team, with the client's
budget). **Refs:** inv = invariant (`docs/RULES.md` §1); VA = VISION-ALIGNMENT.md; DB = DATABASE.md; OD =
STATE open decision. **Page shorthand** in the last section: B★ and B01-B10 are Founder BRD pages,
P01-P12 are build-plan pages, and [n] is spec [n].

## Totals

| Status | Founder BRD (11) | Build plan (12) | Specs (221) | All (244) |
|---|---:|---:|---:|---:|
| ✅ aligned | 0 | 0 | 37 | **37** |
| 🔁 aligned-changed | 0 | 1 | 33 | **34** |
| ⚠️ gap | 7 | 9 | 95 | **111** |
| ❌ conflict | 4 | 1 | 5 | **10** |
| ❓ needs-client | 0 | 1 | 18 | **19** |
| 🧩 other-team | 0 | 0 | 8 | **8** |
| ⏭ later-phase | 0 | 0 | 25 | **25** |
| **Needs action (gap + conflict + needs-client)** | 11 | 11 | 118 | **140** |

## Founder BRD pages (L10-4640)

| # | Page | Status | Owner | Note | Refs |
|---:|---|---|---|---|---|
| 1 | [★] Complete Founder BRD (End-to-End) · L10 | ❌ conflict | shared | Autonomous apparel agent owning listings until LIVE. Conflicts C1-C4; provenance, compliance, variants, issues, publish ledger unplanned. | C1-C4; G1-G8, G11-G19; Q1-Q9; VA §2 |
| 2 | [01] Product Vision, 19-Step Flow & Scope (§1–4) · L2287 | ❌ conflict | shared | Scope matches. 'Listed' = confirmed LIVE (C2); polling, versioned schemas, account profile, human tasks, error classes unplanned. | C1, C2; G3, G5, G7, G18, G19; Q2, Q4 |
| 3 | [02] Brand Onboarding & Catalog Intake (§5) · L2520 | ⚠️ gap | BE | Brand compliance profile not modelled (G2). Brand vs seller account (Q6) and listings as intake source (Q20) await the client. | G2, G19; Q6, Q20; DB §5.1 |
| 4 | [03] Master Apparel Data & Variants (§6–7) · L2673 | ⚠️ gap | BE | Families decided (STATE). Missing: colour level and size×colour matrix (G8), seller vs normalized values (G1), identity (G2). | G1, G2, G7, G8; STATE Decided; DB §5.2 |
| 5 | [04] Media, Content & Canonical Record (§8–10) · L2934 | ⚠️ gap | BE | Content objects, audit event align. Gaps: media roles (G12), claim rule (G1, G6), mapping (G7), issues (G5), overall status (G3), compliance (G2). | G1-G3, G5-G7, G12 |
| 6 | [05] Business Data Model & 14-Step Engine (§11–12) · L3129 | ⚠️ gap | BE | Listing job = job_runs. Missing: versioned schema (G7), attribute provenance (G1), per-field validation results, submissions (G3). | G1, G3, G5, G7; VA §4.9 |
| 7 | [06] Status Lifecycle & Dashboard Specs (§13–14) · L3412 | ❌ conflict | shared | BRD 14 statuses vs our 9. Proposal: keep 9, add marketplace_state/paused/archived and a BRD-named display projection. | C3, C2; G3, G5; Q3; Canon listing-status row |
| 8 | [07] Error Management, Anti-Loop & HITL (§15–16) · L3590 | ⚠️ gap | shared | Anti-loop matches inv 9-10. Unplanned: 11 error classes (G18), ask-once-then-resume tasks (G5), auto-fix resubmission (Q4). | G5, G18; Q4; C1 |
| 9 | [08] Quality Gates & Legal Metrology Matrix (§17–19) · L3717 | ⚠️ gap | BE | Nine gates need product facts in validate() (G9) and richer rules (G6). Legal Metrology fields absent (G2); blocking set: Q8. | G2, G6, G9; Q8 |
| 10 | [09] Master Field Dictionary & E2E Walkthrough (§20–21) · L3898 | ❌ conflict | shared | 64-column template vs our sku/title/category + JSON attributes (C4). The §21 walkthrough should be our E2E fixture; fails today. | C4; G1, G2, G8; VA §4.13 |
| 11 | [10] Acceptance Criteria, Metrics & Lead Handoff (§22–26) · L4493 | ⚠️ gap | shared | At least 6 of 17 acceptance criteria fail today; the 10 metrics lack raw events; no locale (G15). Inference policy is Q9. | G3-G5, G15; Q9; VA §4.13 |

## 45-Day Build Plan pages (L4641-7064)

| # | Page | Status | Owner | Note | Refs |
|---:|---|---|---|---|---|
| 12 | [01] 45-Day Plan & Definition of Done · L4641 | ⚠️ gap | shared | DoD, invariants and loop match; Redis → procrastinate (ADR-0004). Operational analytics match TEAM-QUESTIONS T14's default (S3 stubs the endpoints), though no step builds them yet (Q25). Gaps: 'approve or reject with audit trail' has no reject contract (G4); per-field publish results are only Proposed (G3). | G3, G4; T14; Q25; ADR-0004, ADR-0008 |
| 13 | [02] Phase-1 Scope Lock & Non-Goals · L4723 | ❓ needs-client | client | Scope matches PRD.md §6. Our additions (variants, memberships, tenant lifecycle, auto_publish) and no Redis need sign-off. | Q14; C6; ADR-0004; STATE Decided |
| 14 | [03] Day-by-Day Execution Plan (45 Days) · L4853 | ⚠️ gap | shared | Pre-S1 vs Day-7 milestones (Q25). Unplanned: the brand-guide chunking step (Day 16), image rules as data (Day 24, G6), the failure-injection suite (Day 28). Rollback <15 min is ADR-0009; stack, UAT and the AgentState snapshot changed by ruling (ADR-0001–0005, ADR-0010, Canon seam). Day 45's lone user is C10. | Q12, Q15, Q16, Q25; G6, G13; C8, C10; ADR-0001–0010; DB §8.3 |
| 15 | [04] Team Structure & Solo Sprint · L5538 | ⚠️ gap | shared | Shared schema contract matches; chunking moved to backend (Canon), the Redis/ARQ worker dropped (ADR-0004). The critical path (infra Days 1-3, schema Days 4-5) is missed with no re-baseline (Q25); Days 43-45 UAT has no ruling (C8). | CANON.md:129; ADR-0004; Q18, Q25; C8, Q15 |
| 16 | [05] GCP Tech Stack & Architecture · L5659 | 🔁 aligned-changed | BE | GCP, 4 layers and cloud abstractions match. Python 3.13, PG17, procrastinate + psycopg and the built-in Cloud SQL connection with no VPC connector are ADR rulings; tell the client (TEAM-QUESTIONS T1-T5). | C6, C7; ADR-0001–0004 |
| 17 | [06] Consolidated Data Model & Schema · L5859 | ❌ conflict | BE | Canon's source DDL (tenant_id, memberships, no swatch, cost_usd). Open: flat vs nested CanonicalListing (C5), chunk-type CHECK (G13). | C5; G13; Canon naming rows; DB §5 |
| 18 | [07] State Machine, LangGraph & Rules · L6099 | ⚠️ gap | shared | Stages and in-graph approval/publish replaced by 9 statuses + PipelineStage (Canon). Gaps: 'rejected with feedback' has no reject API (G4; review_decisions exists by ADR-0008); transitions are only Proposed (G16). Who resolves SKU conflicts and unmapped categories is Q18. | G4, G16; Q18; ADR-0008; Canon listing-status and pipeline-stage rows |
| 19 | [08] AI Agent Layer & Brand RAG · L6266 | ⚠️ gap | shared | Seam and RAG infra covered; ~200-word chunks match the DB draft. Open: the escalation port, a chunk-type CHECK (only Proposed), approved listings as examples (G13), the brand-guide parser (no S-step). | G13, C5; VA §4.4, §6.2; Canon AI seam; DB §5.5 |
| 20 | [09] Image Compliance & Adapters · L6405 | ⚠️ gap | BE | Image pipeline matches; the ImageReport attaches to the listing version. Gaps: image rules not data (G6); adapter ABC unwritten (G11). Family publishing is ADR-0007. | G6, G11; ADR-0007 |
| 21 | [10] Security, Multi-Tenancy & Reliability · L6546 | ⚠️ gap | BE | Repository, maker-checker DDL, KMS, RBAC match; FastAPI issues JWTs, procrastinate replaces Redis+ARQ. 'TLS 1.3 enforced everywhere' (L6586) is stated nowhere in our docs ([162]). | Canon D6; ADR-0004; CANON.md:51; VA §4.11 |
| 22 | [11] Testing Strategy, Launch Gate & Risks · L6656 | ⚠️ gap | BE | Test matrix and risks match; gate 10 rollback is ADR-0009; gate 9 UAT becomes the automated walkthrough (ADR-0010, provisional: C8). Gaps: failure injection (rate limits, token expiry, partial publish, network drop) isn't in ADR-0010; gate 6 wants a restore rehearsal under 15 min, the DB draft targets RTO ≤ 2 h. | C8; Q15; ADR-0009, ADR-0010; VA §4.13; DB §9 |
| 23 | [12] API Surface, Dev Setup & Appendix · L6886 | ⚠️ gap | BE | Endpoints under /api/v1; dev setup PG17, no Redis (ADR). ADR-0001's ~$195-205/mo sits inside this page's $150-250; operational /analytics matches T14's default. Gaps: approve carries no rejection or feedback (G4); per-field publish results are only Proposed (G3). | G3, G4; T14; Canon prefix row; ADR-0001, 0003, 0004 |

## Specs [1]-[221] (L7065-13711)

| # | Page | Status | Owner | Note | Refs |
|---:|---|---|---|---|---|
| 24 | [1] System Overview · L7065 | ❓ needs-client | client | Safety chain, 4 layers, Amazon + Flipkart and the canonical model match; confidence scoring is the AI team's Day-21 formula ADR. As at [6], the flow takes catalog Sync as an input (Q20) and loops performance into optimization (C9, Q10). | Q20; C9, Q10; CONTRACTS.md:34 |
| 25 | [2] Architecture Principles · L7303 | ⚠️ gap | BE | Adapters, tenancy, AgentRun logging match; Secret Manager replaces AWS. Gaps: approve has no passed-validation precondition outside the DB draft (G9; DB §7.1 is Proposed); 'adapters own rate limiting' has no outbound limiter (G17). | G9, G17; ADR-0001; inv 2, 5, 7; DB §7.1 |
| 26 | [3] Product Scope · L7379 | ❓ needs-client | client | Non-MVP list matches PRD.md §6. The source contradicts itself on catalog sync: MVP here and in [7], Future in [8] (Q20). | Q20; ARCHITECTURE.md:70 |
| 27 | [4] Users & Roles · L7437 | ❓ needs-client | client | Roles and generator≠approver match; rights per Canon D13 (Q17). One-person brands can never publish (C10, Q5). | C10; Q5, Q17; CANON.md:152 |
| 28 | [5] MVP Boundaries · L7487 | ❓ needs-client | client | Versioning in MVP matches. 'Basic analytics' in MVP and whether performance is v1 need the client (C9, Q10). | C9; Q10; G14 |
| 29 | [6] Architecture Diagram · L7611 | ❓ needs-client | client | Only the Listing Agent is MVP, as in our non-goals. But the 'Current MVP' diagrams take Catalog Sync as an input (Q20) and loop Performance → Optimization Agent (C9, Q10), which we defer. | PRD.md §6; Q20; C9, Q10 |
| 30 | [7] End-to-End User Flow · L7744 | ⚠️ gap | shared | Parallel agents, correction cap, field-only retries and one auth refresh (S13) match. Unplanned: per-row generation fan-out with per-marketplace concurrency (G10, G17), a marketplace-differences preview (G11), a reject API (G4), a reconnect state after the failed refresh (G19). | G4, G10, G11, G17, G19; Q11 |
| 31 | [8] Product Import Flow · L7874 | ❌ conflict | BE | Async CSV + row validation match. Spec requires ≥1 image per row and JPEG/PNG/TIFF; scaffold has optional images and .webp. | new (VA §4.3); ADR-0004; scaffold row_model.py |
| 32 | [9] Marketplace Connection Flow · L7933 | ⚠️ gap | BE | OAuth callback under /api/v1/marketplaces/{m}, state in Postgres, auto-refresh match. 'Persistent failure escalates to user' has no state yet: `reauth_required` is only Proposed (G19). | Canon OAuth rows; ADR-0004; G19; DB §5.4 |
| 33 | [10] Listing Generation Flow · L7973 | 🔁 aligned-changed | shared | Backend pushes context in GenerationContext; brand chunks are pulled through retrieve_brand_chunks (Canon AI seam). | CANON.md:118-119, :125 |
| 34 | [11] Validation Flow · L8016 | ⚠️ gap | shared | Stage order and one ErrorReport match. Stage 2 'category compliance' has no RuleDef type (G6). The example path and code (title, TITLE_TOO_LONG) become content.title and FIELD_TOO_LONG (C5, [122]). | G6, C5; inv 8; Addendum §16; VA §6.2 |
| 35 | [12] Human Approval Flow · L8081 | ⚠️ gap | BE | Approve (ADR-0008), edit-as-version and regenerate match. Missing: a marketplace preview (G11), and a reject with reason and feedback: review_decisions exists by ADR-0008, but no API writes a reject (G4). | G4, G11; ADR-0008; KNOWN-ISSUES.md:15; DB §5.3 |
| 36 | [13] Publishing Flow · L8135 | ⚠️ gap | BE | Approved-only publish on the publish queue matches; ListingResult is drafted in CONTRACTS. The per-field ledger and external_id are only Proposed (G3). | G3; ADR-0004; DB §5.4; catalogus move 1 |
| 37 | [14] Performance Feedback Loop · L8171 | ❓ needs-client | client | Loop is Phase 3 (matches). The site contradicts itself: this page collects and shows performance in MVP, while the plan defers performance tracking to Phase 2 (L6880). Default: Phase 2 (TEAM-QUESTIONS T14); SP-API roles may lack the data. | C9; G14; Q10; T14; BLOCKERS.md:32 |
| 38 | [15] AI Listing Agent Overview · L8193 | 🔁 aligned-changed | shared | Inputs and outputs match GenerationContext and GenerationResult (Canon seam). Its RuleSet and CategoryTemplate are required and resolved before enqueue, so nothing generates without rules; models come from config; brand_id → tenant_id. | Canon AI seam; Addendum §17; ENV.md:90-92; inv 1, 2 |
| 39 | [16] Agent Orchestration · L8348 | 🔁 aligned-changed | AI | Graph ends at quality_check: no approval interrupt or publish node (CANON.md:124). brand_id → tenant_id. | CANON.md:47, :124 |
| 40 | [17] Agent State Machine · L8582 | ⚠️ gap | BE | 17-state lifecycle replaced by 9 statuses + stages (Canon). REJECTED becomes review_decisions (the table is ADR-0008; reject rows need an API, G4); the transition guard is only Proposed (G16, DB §6.1). | G16, G4; C3; ADR-0008; DB §6.1 |
| 41 | [18] Agent Execution Graph · L8688 | 🧩 other-team | AI | AI team's graph; PipelineStage mirrors it, approval outside it. Seam ports: retrieve_brand_chunks, validate_images, emit_node_event. | CANON.md:54, :118-124; DB §6.1 |
| 42 | [19] Agent Run Lifecycle · L8724 | ⚠️ gap | BE | Fields covered (cost is cost_usd); backend is sole writer. 'Re-runnable with the same input' is not met: job payloads hold ids and the context is rebuilt (Q26), as at [139]. | inv 7; CANON.md:56, :113, :118; Q26; DB §5.4 |
| 43 | [20] Context Assembly · L8760 | 🔁 aligned-changed | shared | Backend pushes product, rules, template, prior version, instructions; AI pulls top-8 chunks. No provenance (G1); unversioned templates (G7). | CANON.md:118-125; G1, G7; DB §5.5 |
| 44 | [21] Prompt Construction · L8795 | ⚠️ gap | shared | Prompts are AI team's. 'Never invent' has no backstop: UNSUPPORTED_CLAIM exists but no rule emits it (G1, G6). | G1, G6; CONTRACTS.md:173 |
| 45 | [22] Structured Output · L8847 | ⚠️ gap | shared | Pydantic validation matches. Escalation mini→gpt-4o has no purpose in get_model_credentials; needs an AI-seam Canon PR. | VA §4.4; CANON.md:119; ENV.md:92 |
| 46 | [23] AI Validation · L8883 | ⚠️ gap | BE | Schema, rules and a per-marketplace ImageReport covered. No factual cross-check: no claim rule or product facts in validate() (G1, G6, G9). | G1, G6, G9; Canon AI seam |
| 47 | [24] Correction Loop · L8925 | ✅ aligned | shared | Field-only correction capped at 3 inside the graph, then blocked, matches invariant 9. | inv 9, 10; ADR-0004 §6 |
| 48 | [25] Bounded Retry System · L8957 | ⚠️ gap | shared | Cap 3, backoff, blocked + ErrorReport covered; one refresh then AUTH_EXPIRED (S13) matches '1 auto-refresh, then user'. Gaps: model escalation has no seam purpose; the reconnect state is only Proposed and flags after 3 failures, not 1 (G19). | G19; VA §4.4; ADR-0004 §6; BUILD-SEQUENCE S13; DB §5.4 |
| 49 | [26] Content Agent · L9003 | ⚠️ gap | shared | AI team's agent; the seam pushes product, brand and rules. Gaps: 'the Rules Engine cross-references claims against the product record' has no claim rule or product facts in validate() (G1, G6, G9); the required title elements have no rule type (G6); approved listings as examples are unwired (G13). | G1, G6, G9, G13; Canon AI seam |
| 50 | [27] Keyword Agent · L9178 | ⚠️ gap | shared | backend_keywords → hidden_terms (C5). 'No repeat of visible content' needs a cross_field rule (G6); stuffing is prompt work. | C5; G6; DECISIONS D3 |
| 51 | [28] Attribute Agent · L9232 | 🧩 other-team | AI | AI team's agent emits canonical attributes; adapters map names and values. Value maps as data are G7. | G7; CANON.md:122 |
| 52 | [29] Image Agent · L9280 | ⚠️ gap | shared | Two-stage pipeline matches (backend Pillow + AI vision via validate_images, on GCS). Its per-marketplace thresholds have no image RuleDef (G6); a verdict per marketplace is G12; Flipkart 'Restricted' needs a severity (new). | G6, G12; new; CANON.md:119, :129; ADR-0001 |
| 53 | [30] Brand Voice Agent / Retrieval · L9363 | ⚠️ gap | shared | Embedding model as config matches. Per-artifact chunking and approved listings as examples unplanned (G13). | G13; CANON.md:129; DB §5.5 |
| 54 | [31] Optimization Agent · L9425 | ⏭ later-phase | AI | Phase 3, scheduled, same validation and approval; matches PRD.md §7. Needs v1 review/performance signals (G4, G14). | PRD.md §7; G4, G14 |
| 55 | [32] Future Agent Registry · L9447 | ⏭ later-phase | AI | Future. One pipeline entry point; a new agent = schemas MINOR (AgentName, PipelineStage) + CHECK alter. | CANON.md:54, :109, :130 |
| 56 | [33] Agent Responsibilities Matrix · L9480 | 🧩 other-team | AI | AI team owns the agents; seam and Rules Engine aligned. Open: approve precondition (G9), undefined SEO score (new). | G9, G1, G7, G12, C5; new: SEO score |
| 57 | [34] Listing Generation Responsibilities · L9566 | ⚠️ gap | shared | Agent split matches; Rules Engine is ours. Open: approve precondition (G9); hidden_terms rename has no Canon row (C5). | G9, C5, G1, G7; inv 2, 8 |
| 58 | [35] Canonical Listing Model · L9630 | ❌ conflict | shared | Spec nests content plus category/status/image_reports/version; plan [06] is flat. Ours is nested + hidden_terms, no Canon row (C5). | C5; DECISIONS D3; CONTRACTS.md listing.py |
| 59 | [36] Title Generation · L9686 | ⚠️ gap | shared | Caps and banned terms are rule rows. Gap: the required title elements (brand, product type) have no rule type (G6). Flipkart's per-category caps wait on the launch categories (Q7). | G6; Q7 |
| 60 | [37] Bullet Generation · L9730 | ⚠️ gap | shared | Bullet count and max length are rule data; style is prompt work. 'Factual claims must match product data' has no backstop: no rule emits UNSUPPORTED_CLAIM (G1, G6), as at [21]. | G1, G6; Addendum §16; CONTRACTS.md:173 |
| 61 | [38] Description Generation · L9750 | ⚠️ gap | shared | Prose is AI team's. No description markup contract: plain vs HTML subset, adapter rendering, sanitising, Flipkart rules (new). | new; G6, G11; CONTRACTS.md:222 |
| 62 | [39] Keyword Generation · L9764 | ⚠️ gap | shared | search_terms + hidden_terms covered (C5). 250-byte cap expressible, unseeded; 'not in listing' needs cross_field (G6). | C5; G6; scaffold rules.py:36, :65 |
| 63 | [40] Attribute Generation · L9778 | ⚠️ gap | shared | Required attributes and MISSING_ATTRIBUTE covered; a required_attribute rule can carry allowed_values, which reach the AI in the RuleSet. Optional attributes have no allowed values anywhere, and there are no value maps (G6, G7). | G6, G7; Canon 0003; Addendum §16 |
| 64 | [41] Category Mapping · L9792 | ✅ aligned | BE | Internal slug mapped per marketplace via category_templates (browse node, vertical); unversioned (G7). | CANON.md:96; G7 |
| 65 | [42] Marketplace-specific Content · L9806 | ⚠️ gap | shared | Listings, caps, hidden_terms covered. Missing: a description HTML contract (new) and per-marketplace background rules as data (G6). | G6; new: description format |
| 66 | [43] Listing Versioning · L9854 | ⏭ later-phase | BE | Phase 2. MVP storage matches (versions + validation_runs). Compare UI, rollback-to-approved, per-version performance later. | Canon 0002; CANON.md:152 D13; DB §5.3; C9 |
| 67 | [44] Image Compliance Pipeline · L9872 | ⚠️ gap | BE | Pipeline shape matches. Image rules (white, 1000px, aspect, text/watermark) have no RuleDef type; would be hardcoded (G6). | G6; Addendum §16 |
| 68 | [45] Image Processing Flow · L9912 | ⚠️ gap | BE | Upload, Pillow, vision, ImageReport planned on GCS. Gaps: the tenant-scoped key convention (G12); JPEG/PNG/TIFF by header and EXIF unspecified (the scaffold takes .webp, no TIFF); policies not data (G6). | G6, G12; new: formats; ADR-0001 |
| 69 | [46] Deterministic Image Checks · L9932 | ⚠️ gap | BE | Backend Pillow checks exist. Thresholds not data (G6); formats vs scaffold (.webp, no TIFF); EXIF derivative storage unplanned. | G6, G12; new: formats, EXIF; CONTRACTS.md:392-427 |
| 70 | [47] Vision Model Checks · L9988 | ⚠️ gap | shared | Checks match ImageReport's CheckName; the vision prompt is the AI team's. The per-marketplace policies it is judged against are not data (G6). | G6; CONTRACTS.md:392-427; Canon ownership line |
| 71 | [48] ImageReport Schema · L10011 | ✅ aligned | BE | The contract is a superset (code + check for type, plus checks[] and observed/expected). A report is per (image, marketplace, slot) and is kept per listing version, which is per marketplace (listing_versions.image_reports_json). | CONTRACTS.md:392-427; Canon 0002; DB §5.3 |
| 72 | [49] Image Error Taxonomy · L10059 | ⚠️ gap | BE | All six codes plus four; ImageIssue has severity and suggestion. TEXT_DETECTED is Warning/Error by slot, and with no image RuleDef that severity would be hardcoded (G6). | CONTRACTS.md codes.py, images.py; G6 |
| 73 | [50] Image Correction Loop · L10113 | ✅ aligned | BE | Re-upload through the image endpoints re-runs the checks. Warnings never block: a needs_review listing can be approved, and that approval is audited (ADR-0008), so 'accept warnings' needs no extra action. | BUILD-SEQUENCE S3; ADR-0008; DB §6.1 |
| 74 | [51] Marketplace Image Rules · L10127 | ⚠️ gap | BE | Rules-as-data agreed, but no image RuleDef types, so thresholds would be hardcoded (G6). Flipkart 'restricted' needs severity. | G6; catalogus move 4; Addendum §4, §16 |
| 75 | [52] Image Storage Lifecycle · L10183 | ⚠️ gap | BE | Tenant-scoped key convention, image versions and archiving unspecified; the GCS lifecycle rule covers CSVs only (G12). | G12; ADR-0001 |
| 76 | [53] Brand Voice Architecture · L10197 | ⚠️ gap | shared | Chunk → text-embedding-3-small → HNSW → top-8 covered. Approved listings as brand memory not wired (G13). | G13; Canon 0002/0002b; DB §5.5 |
| 77 | [54] Brand Voice Vectorization · L10234 | ⚠️ gap | BE | BrandChunk store exists. Per-artifact chunking, type vocabulary and embedding approved listings unplanned or proposed (G13). | G13, G2, G6; DB §5.5; VA §4.4 |
| 78 | [55] Brand Chunking · L10248 | ⚠️ gap | BE | Backend owns chunking, but the DB draft uses ~200-word chunks, not per-rule/term/field units; no parser or splitter (G13). | G13, G6; CANON.md:94, :129; DB §5.5 |
| 79 | [56] Embedding Pipeline · L10296 | ⚠️ gap | BE | Model is config, 1536 dims, brand_embed job exists. Re-embed on update and batch vs incremental unspecified (new). | new; Canon D12; ENV.md:93-94; DB §5.5 |
| 80 | [57] Vector Retrieval · L10310 | ⚠️ gap | shared | pgvector cosine top-8 in one database matches. The relevance-threshold step has no value or agreed owner: the port takes no threshold, and AI-side filtering on similarity is only proposed. | Canon 0002b; CANON.md:119; DB §5.5; VA §6.2 |
| 81 | [58] Retrieval Context · L10337 | 🔁 aligned-changed | shared | BrandContextContract replaced by retrieve_brand_chunks → Sequence[BrandChunkDTO]; brand_id is tenant_id. | CANON.md:119, :122 |
| 82 | [59] Brand Context Contract · L10351 | ⚠️ gap | shared | Port returns BrandChunkDTOs with similarity. The relevance threshold has no owner, and the chunk_ids kept 'for tracking' are never recorded per version (new). | CANON.md:119, :122; VA §6.2; new |
| 83 | [60] Approved Listing Memory · L10390 | ⚠️ gap | BE | No approve → brand_embed trigger and no weight column; 'high-performing' needs performance data, which is Phase 2 (G13, C9). | G13; C9, G14 |
| 84 | [61] Brand Glossary · L10404 | ⚠️ gap | BE | tenants.glossary exists; per-term chunks and Rules Engine enforcement (glossary → tenant banned_terms) unplanned (G13, G6). | G13, G6; VA §4.4 |
| 85 | [62] Brand Compliance Rules · L10418 | ⚠️ gap | BE | Brand compliance chunks are planned. Gaps: brand rules have no write path, and the DB draft even proposes no tenant rules in v1; 'no organic claim without certification' needs claim_requires_attribute (G6). | G6; Addendum §4, §17; DB §5.2 |
| 86 | [63] AI Marketplace OS · L10432 | ⏭ later-phase | shared | Future vision. Listing Agent is our scope; other agents and marketplaces are non-goals. 'No rewrite' = CHECK ALTER + MINOR. | PRD.md §6; VA §4.8 |
| 87 | [64] Marketplace Abstraction · L10474 | ✅ aligned | BE | Only adapters touch marketplace APIs; the canonical model stays neutral (invariant 4). | inv 4 |
| 88 | [65] Canonical Data Model · L10488 | ⚠️ gap | BE | Invariant 4 and CanonicalListing cover it. Attribute keys and value formats undefined (G1, G7); shape lacks a Canon row (C5). | G1, G7, C5; inv 4 |
| 89 | [66] Marketplace Adapter Pattern · L10506 | ⚠️ gap | BE | ListingResult is drafted and the ABC is spec-given; FakeAdapter only so far. Gaps: 'adapters own rate limiting' has no limiter (G17); the ABC with get_performance and a review preview() is unwritten (G11). | G11, G17; Canon AI seam (FakeAdapter) |
| 90 | [67] Amazon Adapter · L10537 | ⚠️ gap | BE | LWA OAuth, encrypted tokens and auto-refresh match. No per-account outbound rate limiter from published quotas (G17). | G17; inv 6 |
| 91 | [68] Flipkart Adapter · L10565 | ✅ aligned | BE | Same interface, Flipkart OAuth (Canon redirect URIs), field and category mapping inside the adapter, and error mapping to the ErrorReport are all planned (inv 4, 8). API access and a sandbox are a client blocker (Q16), with catalogus move 7 as the fallback. | inv 4, 8; Canon OAuth rows; Q16; catalogus move 7 |
| 92 | [69] Future Marketplace Adapter · L10579 | ⏭ later-phase | BE | Future non-goal. Needs frozen adapter ABC (G11), value maps (G7), image rules (G6), marketplace CHECK ALTER. | G6, G7, G11; Q23 |
| 93 | [70] Marketplace Rule Registry · L10597 | ⚠️ gap | BE | rules table with marketplace, category, rule_def, severity and version, loaded per target. 'Updating rules needs no deployment' fails: only a migrator CLI writes them, with no write API (as at [93]). | new; Addendum §4; DB §5.2 |
| 94 | [71] Marketplace Category Mapping · L10611 | ⚠️ gap | BE | Mapping via category_templates planned. Gap: 'category selection decides which image policies apply', and image policies aren't data (G6). The two launch categories are Q7. | G6, G7; Q7 |
| 95 | [72] Marketplace Field Mapping · L10625 | ⚠️ gap | BE | Canonical→marketplace field/value maps (navy blue → Navy) have no home; templates hold required/optional only (G7). | G7 |
| 96 | [73] Marketplace Error Mapping · L10639 | ✅ aligned | BE | Adapters map marketplace errors to the unified ErrorReport. A data-driven raw-code table is G18. | inv 8; G18 |
| 97 | [74] Catalog Agent · L10653 | ⏭ later-phase | AI | Future agent; out of MVP. | ARCHITECTURE.md:73 |
| 98 | [75] Revenue Operations Agent · L10669 | ⏭ later-phase | AI | Future; depends on pricing, ads and sales attribution. No backend work now. | ARCHITECTURE.md:68, :73 |
| 99 | [76] Pricing Agent · L10685 | ⏭ later-phase | AI | Future; the BRD excludes pricing optimization and competitor scraping is a non-goal. | PRD.md §6; VA §2 |
| 100 | [77] Ads Agent · L10701 | ⏭ later-phase | AI | Future; the BRD excludes ads. Would need advertising-API roles beyond those filed. | VA §2; BLOCKERS.md:32 |
| 101 | [78] Ranking Agent · L10717 | ⏭ later-phase | AI | Future. performance.ranking_position exists but ingestion is undecided (G14, C9); competitor scraping is a non-goal. | G14, C9; CANON.md:94 |
| 102 | [79] Inventory Agent · L10733 | ⏭ later-phase | AI | Future; inventory is a BRD non-goal. Listed in the PRD.md §6 non-goals. | VA §4.14 |
| 103 | [80] Returns Agent · L10749 | ⏭ later-phase | AI | Future; returns are BRD not-v1 and no returns data ingestion is planned. | VA §2, §4.14 |
| 104 | [81] Optimization Agent (Future) · L10765 | ⏭ later-phase | AI | Phase 3 per PRD.md §7. v1 should capture reject reasons and performance so the agent has training signal (G4, G14). | PRD.md §7; G4, G14, C9 |
| 105 | [82] Agent Dependency Graph · L10779 | ⚠️ gap | BE | Order matches S1-S14 up to rules. No S-step for brand context; adapters, publish, analytics unsequenced (see [211]). | new; BUILD-SEQUENCE; Q25; C9 |
| 106 | [83] Agent Registry · L10813 | ⏭ later-phase | shared | Future. agent_runs already gives per-agent history and cost; no registry table or on/off switch planned. | DB §5.4; CANON.md:94 |
| 107 | [84] Data Architecture · L10827 | ✅ aligned | BE | One Postgres + pgvector for relational and vector data, tenant_id on every table. | ADR-0003; DB §2, §4 |
| 108 | [85] Database Schema · L10884 | ⚠️ gap | BE | Spec DDL mapped (tenant_id, 9 statuses, cost_usd). external_id only Proposed (G3); chunk-type CHECK missing (G13). | G3, G13; Canon 0002; DB §5.3, §5.5 |
| 109 | [86] Entity Relationship Diagram · L10965 | ✅ aligned | BE | Tenant id on every entity, FKs and indexes match; the Canon adds composite tenant FKs and RLS. | DB §4 |
| 110 | [87] Tenant Model · L10995 | ✅ aligned | BE | tenant == brand matches; tone text plus guidelines and glossary as JSONB (DB §5.1 keeps the spec types); embedding_refs is the brand_chunks relation. | CANON.md:47; DB §5.1, §5.5 |
| 111 | [88] Brand Model · L11009 | ⚠️ gap | BE | tone, guidelines and glossary covered (JSONB kept, DB §5.1), though glossary is an array of {term, preferred}, not a map. The brand compliance profile is missing (G2); Q6 is open. | G2; Q6; DB §5.1 |
| 112 | [89] Product Model · L11023 | ⚠️ gap | BE | tenant_id replaces brand_id; images move to an images table with storage keys. 'The product is the source of truth for factual claims' lacks provenance and a claim rule (G1, G6). | CANON.md:47; Canon 0001/0002; G1, G6 |
| 113 | [90] MarketplaceAccount Model · L11037 | 🔁 aligned-changed | BE | tenant_id, KMS-wrapped AES-256-GCM token_enc, adapter-only refresh. Status Proposed; settings, needs_reconnect G19. | G19; DB §5.4; inv 6 |
| 114 | [91] Listing Model · L11051 | ⚠️ gap | BE | listings with 9 lowercase statuses (deliberate). external_id is only Proposed in DATABASE.md, not in the Canon DDL (G3). | G3, G16, C3; DB §5.3 |
| 115 | [92] ListingVersion Model · L11065 | ✅ aligned | BE | listing_versions with content_json and image_reports_json; every edit is a new version, immutable after approval. | Canon 0002/0004; DB §5.3 |
| 116 | [93] Rules Model · L11079 | ⚠️ gap | BE | Superset with versions. New: only migrator writes platform rules and no write API exists, so updates need a release. | new; Addendum §4, §16; DB §5.2; G6 |
| 117 | [94] ValidationRun Model · L11093 | 🔁 aligned-changed | BE | validation_runs per version, with rules_version and counts. source takes the seven ErrorSource strings, not the spec's rules/vision/api (Canon forbids those). | CANON.md:58; Addendum §19; DB §5.3 |
| 118 | [95] AgentRun Model · L11107 | 🔁 aligned-changed | BE | Superset; cost renamed cost_usd NUMERIC(12,6); backend is sole writer, one row per model call. | CANON.md:56, :113; DB §5.4 |
| 119 | [96] Performance Model · L11121 | ❓ needs-client | shared | Table exists; no ingestion job or adapter method (G14). The site contradicts itself on MVP collection (here, L11131, vs the plan's Phase 2, L6880); default Phase 2 (T14). SP-API roles may not yield impressions/clicks/rank. | G14; C9; Q10; T14; BLOCKERS.md:32 |
| 120 | [97] AuditLog Model · L11135 | ⚠️ gap | BE | Append-only audit_log with actor, action, entity, before/after. Rejection has no record (G4), and platform rule changes can't be logged under the Canon's NOT NULL tenant_id (fix only Proposed). | Canon 0002/0004; G4, G11; KNOWN-ISSUES.md:33; DB §5.1 |
| 121 | [98] BrandChunk Model · L11149 | ⚠️ gap | BE | vector(1536), HNSW, <=>; tenant_id replaces brand_id. The type vocabulary is only Proposed, with no CHECK (G13), and the DB draft drops updated_at, which Canon 0002 keeps (new). | G13; Canon 0002/0002b; DB §5.5 |
| 122 | [99] Image Model · L11163 | 🔁 aligned-changed | BE | Same shape as the spec: one compliance_status plus image_report_json per image, which is all the spec's own model has for 'per marketplace'. storage_key replaces s3_url and tenant_id is added (Canon 0002). | Canon 0002; Canon ImageSlot row; DB §5.2 |
| 123 | [100] Error Model · L11177 | ✅ aligned | BE | As the spec says: ErrorReports as JSONB on validation_runs and agent_runs (Canon 0002). source is a column; field, code, marketplace and retryable sit in each ErrorItem. An errors API is [114]. | Canon 0002; Addendum §19; DB §5.3, §5.4 |
| 124 | [101] Database Indexing Strategy · L11191 | ✅ aligned | BE | Index plan matches; HNSW chosen (the spec allows IVFFlat or HNSW). | CHECKLISTS.md:149; Canon 0002b |
| 125 | [102] Vector Storage · L11209 | ✅ aligned | BE | pgvector at MVP scale; a separate vector DB is a named non-goal. | PRD.md §6 |
| 126 | [103] API Architecture · L11223 | ❓ needs-client | client | Routes under /api/v1, async, JWT, 202 for long work. The site consistently fans valid import rows into generation (L7806, L7929, L11249, L11345, L12075); VA's default 'no' departs from that (Q11, G10). /analytics as performance + attribution is Q10. | Q11, Q10; G10; Canon prefix row |
| 127 | [104] Product APIs · L11303 | 🔁 aligned-changed | BE | POST /api/v1/products; images uploaded separately through signed GCS URLs, not s3:// URLs in the body. | Canon prefix row; BUILD-SEQUENCE S3 |
| 128 | [105] Bulk Import APIs · L11335 | ❓ needs-client | BE | CSV upload, validation, batch tracking covered as a 202 job; row errors in the ImportReport. 'Enqueues each valid row for generation' is consistent across the site; VA's default 'no' departs from it (Q11, G10). | Q11; G10; Addendum §19-20; DB §5.4 |
| 129 | [106] Listing Generation APIs · L11349 | 🔁 aligned-changed | BE | POST /api/v1/listings/generate → 202 JobAccepted; poll GET /api/v1/jobs/{id}. | CANON.md:60; Canon prefix row |
| 130 | [107] Validation APIs · L11381 | 🔁 aligned-changed | BE | POST /api/v1/listings/validate returns an ErrorReport and is callable on its own. | Canon prefix row; inv 8 |
| 131 | [108] Approval APIs · L11395 | ⚠️ gap | BE | Maker-checker approve covered (ADR-0008); the approver comes from the JWT, not a body reviewer ID. Gap: no contract carries a reject decision or feedback (G4). | G4; ADR-0008; DB §5.3, §7.1 |
| 132 | [109] Publishing APIs · L11409 | 🔁 aligned-changed | BE | POST /api/v1/listings/publish on approved listings only, 202 + job. It should also assert a passed validation (G9). | G9; Canon prefix row |
| 133 | [110] Marketplace Connection APIs · L11423 | 🔁 aligned-changed | BE | POST /api/v1/marketplaces/{m}/connect and GET …/callback (plural, frozen redirect URIs); OAuth state in Postgres. | Canon OAuth rows; ADR-0004 |
| 134 | [111] Analytics APIs · L11437 | ❓ needs-client | client | GET /analytics as performance + sales attribution clashes with plan's Phase-1 operational metrics; 501 stubs only (C9, Q10). | C9; Q10; BUILD-SEQUENCE S3 |
| 135 | [112] Image APIs · L11451 | 🔁 aligned-changed | BE | Upload through /api/v1 images upload-url\|upload to GCS; GET images/{id}/report returns the ImageReport. | ADR-0001; CONTRACTS.md:415-427 |
| 136 | [113] Agent APIs · L11465 | ⚠️ gap | BE | Job polling exists; history and cancel unplanned. Proposal: POST /api/v1/jobs/{id}/cancel for queued jobs. | VA §4.10; CANON.md:51 |
| 137 | [114] Error APIs · L11479 | ⚠️ gap | BE | No error endpoint planned; codes live inside JSONB with no index. VISION-ALIGNMENT proposes GET /api/v1/errors after S14. | VA §4.9; C9; DB §5.3 |
| 138 | [115] Webhook Architecture · L11493 | ⏭ later-phase | BE | Phase 2 in the source and in our plan; v1 polls job_runs. | VA §4.10 |
| 139 | [116] API Authentication · L11507 | 🔁 aligned-changed | BE | FastAPI is the sole JWT issuer (HS256, frozen claims); NextAuth may only wrap our tokens. | Canon D6 (CANON.md:152); BUILD-SEQUENCE S10 |
| 140 | [117] API Authorization · L11521 | ❓ needs-client | client | Four roles with per-route require_role match. Rights differ: Canon D13 lets reviewers regenerate and operators publish; the spec gives operators import/generate and reviewers approve/reject. Q17 is unconfirmed. | Q17; CANON.md:152 D13; BUILD-SEQUENCE S10 |
| 141 | [118] API Versioning · L11535 | 🔁 aligned-changed | BE | Spec: unversioned MVP, /v1 in Phase 2. The Canon versions from day one under /api/v1; ARCHITECTURE.md:71 is stale. | CANON.md:33-34, :130, :138 |
| 142 | [119] Rules Engine · L11549 | ⚠️ gap | BE | Deterministic, never an LLM. Only 5 rule types; category compliance, allowed values, conditional, cross-field missing (G6). | G6; Addendum §16; inv 2 |
| 143 | [120] Validation Architecture · L11607 | ✅ aligned | BE | Schema → Rules Engine → vision, each emitting structured errors. | inv 8; CANON.md:120 |
| 144 | [121] Structured Error Contract · L11625 | ✅ aligned | BE | One ErrorReport shape matches (inv 8). Add MAKER_CHECKER_VIOLATION and fix the path validator in S2. | inv 8; KNOWN-ISSUES.md:26 |
| 145 | [122] Error Source Taxonomy · L11676 | ✅ aligned | BE | Error sources and example codes map onto ErrorSource and codes.py. | CONTRACTS.md codes.py |
| 146 | [123] Retry Classification · L11736 | ✅ aligned | BE | The four classes follow from source + retryable: queue backoff for 429/5xx (ADR-0004 §6), in-graph field correction (inv 9), non-retryable errors surfaced to the user (AUTH_EXPIRED, image issues), dead letters and Sentry alerts for system failures. Raw marketplace codes as data are G18 (BRD). | ADR-0004 §6, §7; inv 9; BUILD-SEQUENCE S11, S13; G18 |
| 147 | [124] Human Approval Gate · L11778 | ⚠️ gap | shared | Mandatory approval matches (inv 3, ADR-0008). No marketplace-differences preview (G11); no API records a reject with feedback (G4). | G4, G11; C1; ADR-0008 |
| 148 | [125] Maker-Checker Model · L11792 | ✅ aligned | BE | Exceeded: the generator and the editor can't approve, enforced in the API and by a DB trigger (ADR-0008, Canon 0004). One-person brands belong to [4] (C10, Q5). | ADR-0008; Canon 0004; DB §7.1 |
| 149 | [126] Audit Trail · L11808 | ❓ needs-client | client | Append-only audit_log planned. 'Never deleted' vs DPDP tenant erasure needs an erasure ADR and client/legal input. | new; catalogus move 6; KNOWN-ISSUES.md:33-34 |
| 150 | [127] Quality Scoring · L11822 | 🧩 other-team | AI | Advisory, never bypasses rules (matches). The quality_check node and its formula (the Day-21 AI ADR) are the AI team's; our seam is the frozen, advisory confidence_score. This page names no historical performance. | CONTRACTS.md:34; Canon AI seam |
| 151 | [128] Confidence Scoring · L11845 | 🧩 other-team | AI | AI team owes the formula ADR by Day 21; advisory, approve ignores it. Historical performance hinges on C9/Q10. | CONTRACTS.md:34, :921; C9, Q10 |
| 152 | [129] Async Processing · L11859 | 🔁 aligned-changed | BE | 202 + job id + polling matches; procrastinate on Postgres replaces Redis + ARQ (ADR-0004, spike-gated). | ADR-0004; CANON.md:60 |
| 153 | [130] Queue Architecture · L11887 | ⚠️ gap | BE | Queues default and publish with separate concurrency replace per-type queues (ADR-0004), with no Redis cache. 'Per-marketplace concurrency limits' have no ruling or design (G17). | ADR-0004; ADR-0001; G17 |
| 154 | [131] Job Lifecycle · L11901 | ⚠️ gap | BE | JobState replaces QUEUED…TIMEOUT; timeouts record JOB_TIMEOUT; a requeue inserts a new row. 'Full context preserved, re-runnable with the same input' fails: payload_json holds ids only (Q26). | CANON.md:51; ADR-0004 §4, §5, §7; Q26; DB §5.4 |
| 155 | [132] Retry Strategy · L11915 | ⚠️ gap | shared | Correction cap, backoff and one auth refresh (S13) match. Gaps: the partial-field retry cap of 3 is written nowhere; JSON escalation has no seam purpose; the reconnect state is only Proposed and flags after 3 failures (G19). | G19; VA §4.4, §4.10; BUILD-SEQUENCE S13; DB §5.4 |
| 156 | [133] Exponential Backoff · L11963 | ⚠️ gap | BE | ADR-0004 §6 backs off on 429/5xx, but the 1 s start, doubling, jitter and 60 s cap exist only in the pre-Canon ARQ text of S13. Pin them in the publish ADR. | ADR-0004 §6; BUILD-SEQUENCE S13; G17 |
| 157 | [134] Rate Limiting · L11977 | ⚠️ gap | BE | Backoff planned, but no outbound per-marketplace limiter; ADR-0005 limits inbound only. G17 proposes a Postgres token bucket. | G17; ADR-0005 |
| 158 | [135] Marketplace Throttling · L11991 | ⚠️ gap | BE | No per-account outbound limiter; in-process semaphores can't span worker instances. Postgres token bucket proposed (G17). | G17, G11; ADR-0004 |
| 159 | [136] Idempotency · L12005 | ⚠️ gap | BE | Our keys cover our API; the per-field marketplace key is only Proposed (DB §5.4). Nothing verifies that Amazon or Flipkart dedupe on it, so a retry should read marketplace state first (G3). | G3; DB §5.4; CHECKLISTS.md:254 |
| 160 | [137] Partial Failure Handling · L12021 | ⚠️ gap | BE | Retry-only-failed-fields is invariant 10 and ListingResult is drafted, but the per-field ledger (publish_field_results) is only Proposed (G3, catalogus move 1). | G3; inv 10; DB §5.4 |
| 161 | [138] Dead Letter / Failed Jobs · L12037 | ⚠️ gap | BE | Dead letters in job_runs with ErrorReport and a parent_job_id chain match. But payload_json holds ids only, so the input context isn't kept (Q26), and who may inspect and requeue is open (Q18). | ADR-0004 §7; Q26, Q18; DB §5.4, §11.1 #17 |
| 162 | [139] Job Reprocessing · L12051 | ⚠️ gap | BE | Only input_digest is stored; a retry rebuilds context, so 'same input' replay isn't guaranteed (Q26). | Q26; CANON.md:118 |
| 163 | [140] Batch Processing · L12065 | ⚠️ gap | BE | Per-row validation matches. Fan-out to generation and per-row approval after import are unplanned (G10, Q11). | G10; Q11 |
| 164 | [141] Parallel Agent Runs · L12079 | ⚠️ gap | shared | Parallel agents and bounded concurrency match. Per-marketplace limits to avoid throttling are missing (G17). | G17 |
| 165 | [142] Model Routing · L12093 | ⚠️ gap | shared | Cheapest-model default matches; DeepSeek deferred. No escalation port; at budget cap we stop, spec implies degrade (Q19). | Q19; VA §4.4; CANON.md:71, :119 |
| 166 | [143] Model Selection Policy · L12160 | ⚠️ gap | shared | gpt-4o-mini default, embedding model and logging covered; DeepSeek deferred. Escalation to gpt-4o has no seam purpose. | ENV.md:90-93; CANON.md:71, :119; VA §4.4 |
| 167 | [144] Model Fallback · L12178 | ⚠️ gap | shared | Repair, re-prompt, blocked match; each step logged in agent_runs. Escalation to gpt-4o has no seam purpose. | VA §4.4; CANON.md:119 |
| 168 | [145] Heavy Reasoning Escalation · L12192 | ⚠️ gap | shared | Policy is the AI team's, but get_model_credentials has no escalation purpose. agent_runs already records the model per call. | VA §4.4, §6.2; CANON.md:119 |
| 169 | [146] JSON Validation · L12206 | ✅ aligned | shared | Every output validated against Pydantic models from mkos_schemas. | CANON.md:120 |
| 170 | [147] Structured Output Repair · L12220 | 🧩 other-team | AI | Repair is AI team's; each re-prompt returns an AgentRunRecord. Seam gap: no escalation purpose (LLM_MODEL_ESCALATION set). | CANON.md:113, :119; ENV.md:92 |
| 171 | [148] Token Management · L12237 | ⚠️ gap | BE | Tokens recorded per run. Budgets exist only globally and per tenant; per-agent and per-marketplace budgets unplanned (Q19). | Q19; ENV.md:97-98 |
| 172 | [149] AI Cost Optimization · L12251 | ⚠️ gap | shared | Routing, top-8, field retries match. Unplanned: skip-unchanged images, caching, batch embedding; listing cloning is a client ask. | new; ADR-0004 §8 |
| 173 | [150] Prompt Versioning · L12274 | ⏭ later-phase | AI | Phase 2. agent_runs.prompt_version exists now; ask the AI team to fill it from day one. | VA §4.14; CANON.md:94 |
| 174 | [151] Model Observability · L12288 | 🔁 aligned-changed | BE | All fields recorded per AgentRun; cost renamed cost_usd (Canon). | inv 7; CANON.md:56 |
| 175 | [152] AI Evaluation · L12302 | ⏭ later-phase | shared | Phase 2. Raw data captured in v1; approve-without-edit needs review_decisions (G4); client wants some in v1 (C9). | C9; G4 |
| 176 | [153] AI Quality Metrics · L12316 | ⏭ later-phase | shared | Phase 2; raw data captured now. The client plan wants some operational metrics in v1 (C9, Q10). | C9; Q10; G4 |
| 177 | [154] Security Architecture · L12364 | ⚠️ gap | BE | Roles, maker-checker match; FastAPI issues JWTs, Secret Manager replaces AWS, Cloud Run sits behind the LB. 'TLS everywhere' is stated nowhere for DB and storage ([162]). | Canon D6; ADR-0001; VA §4.11 |
| 178 | [155] Multi-Tenancy · L12444 | ✅ aligned | BE | tenant_id filter in the base repository, taken from the JWT only. | inv 5; DB §4 |
| 179 | [156] Tenant Isolation · L12460 | ✅ aligned | BE | Exceeded: repository filter, ORM guard, RLS and composite FKs; tenant from the verified JWT tid; CI isolation gate. | inv 5; Canon 0005; BUILD-SEQUENCE S9 |
| 180 | [157] Authentication · L12474 | ❓ needs-client | client | JWT claims validated in FastAPI match. The spec wants email/password and OAuth logins; v1 plans email + password only (Q21). | Q21; BUILD-SEQUENCE S10 |
| 181 | [158] Authorization · L12488 | ✅ aligned | BE | Four roles, per-route RBAC and maker-checker (API + DB trigger) planned; rights await Q17. | Q17; DB §5.1; Canon 0004 |
| 182 | [159] Marketplace OAuth · L12502 | ✅ aligned | BE | Adapter-owned OAuth, encrypted storage and auto-refresh. | inv 6; Canon OAuth rows |
| 183 | [160] Token Encryption · L12516 | 🔁 aligned-changed | BE | AES-256-GCM, decrypted in memory only inside adapters. Keys in Cloud KMS (envelope), not AWS Secrets Manager (ADR-0001). | ADR-0001; DB §5.4; inv 6 |
| 184 | [161] Secrets Management · L12530 | 🔁 aligned-changed | BE | GCP Secret Manager replaces AWS. ADR-0001 injects secrets as env vars, so a rotation takes effect when a new revision starts, not with a code change; the spec's own 'fetched at startup and cached' needs a restart too. Tell the client. | ADR-0001; VA §4.11 |
| 185 | [162] Data Encryption · L12544 | ⚠️ gap | BE | Encryption at rest is default on Cloud SQL/GCS. TLS for DB and storage is stated nowhere; state it in ADR-0001/S4. | VA §4.11; ADR-0001 |
| 186 | [163] API Security · L12559 | ✅ aligned | BE | Per-tenant/IP rate limits (Postgres-backed), Pydantic validation, explicit CORS and a 10 MiB body cap. | ADR-0005; CANON.md:83 |
| 187 | [164] Auditability · L12577 | ✅ aligned | BE | audit_log records who, what, entity, when, before/after. | Canon 0002/0004 |
| 188 | [165] AI Data Security · L12591 | ❓ needs-client | client | Tenant-scoped prompts and no credentials in prompts match. AI-provider retention and region review is a client call (Q22). | Q22; inv 6 |
| 189 | [166] Prompt Injection Considerations · L12605 | ⚠️ gap | shared | Product text is untrusted and output is validated. 'Rules catch malicious content' needs the pattern rule (G6); CanonicalListing is a WireModel (extra="ignore"), so unexpected fields are dropped, not rejected (new). | G6; new; CANON.md:120, :122; CONTRACTS.md base.py |
| 190 | [167] Marketplace Credential Security · L12619 | ⚠️ gap | BE | Encrypted, per tenant, adapter-only. The credential-access audit event is missing (G11). | G11; inv 6 |
| 191 | [168] Logging · L12633 | ✅ aligned | BE | Structured JSON logs with request_id and tenant_id. | BUILD-SEQUENCE S11 |
| 192 | [169] Metrics · L12647 | ✅ aligned | BE | Listed metrics covered by /internal/ops pages and proposed SLO defaults; no Prometheus (plan non-goal). | CHECKLISTS.md:365, :385 |
| 193 | [170] Tracing · L12667 | ⚠️ gap | BE | OTel to Cloud Trace planned, but only request_id enters jobs; carry W3C traceparent in job payloads (S11). | VA §4.12; ADR-0001 |
| 194 | [171] Agent Observability · L12681 | 🔁 aligned-changed | BE | All fields covered; cost is cost_usd; tokens_in/tokens_out keep the spec names (DB §5.4). The backend is sole writer. | inv 7; CANON.md:56, :113; DB §5.4 |
| 195 | [172] AI Cost Observability · L12695 | ✅ aligned | BE | Cost per run, listing, tenant and model; /internal/ops/cost page and daily budgets. | BUILD-SEQUENCE S14; ENV.md:97-98 |
| 196 | [173] Marketplace API Observability · L12709 | ✅ aligned | BE | adapter.call log line and OTel span with rate_limit_remaining. | CHECKLISTS.md:380 |
| 197 | [174] Error Monitoring · L12723 | ✅ aligned | shared | Sentry in FE and BE with alert rules. | ADR-0001; BUILD-SEQUENCE S11 |
| 198 | [175] Sentry · L12737 | ✅ aligned | shared | Backend Sentry planned; FE Sentry is the FE team's. S11's ArqIntegration/RedisIntegration are stale after ADR-0004. | ADR-0001; ENV.md:127-128; ADR-0004 |
| 199 | [176] OpenTelemetry · L12751 | ⚠️ gap | BE | FastAPI/DB tracing planned (psycopg). Gaps: agent spans (pipeline has no OTel), traceparent across jobs, metrics pipeline. | new; VA §4.12; CANON.md:105, :119 |
| 200 | [177] AgentRun Telemetry · L12765 | 🔁 aligned-changed | BE | Correlation chain matches; brand_id collapses into tenant_id. | CANON.md:47; BUILD-SEQUENCE S11 |
| 201 | [178] Recommended Tech Stack · L12779 | 🔁 aligned-changed | shared | Matches except by ADR: GCP, Python 3.13, PG17, procrastinate, FastAPI JWT, DeepSeek deferred. Tell the client (C6, C7). | C6, C7; ADR-0001–0005 |
| 202 | [179] Frontend Architecture · L12879 | 🧩 other-team | FE | FE team's Next.js pages; seams are our /api/v1 endpoints and @mkos/schemas. | Canon naming rows |
| 203 | [180] Backend Architecture · L12893 | 🔁 aligned-changed | BE | Routers, services and repositories kept; agents live in packages/pipeline (Canon), adapters in apps/api. | Canon AI-package row; CANON.md:105 |
| 204 | [181] AI Infrastructure · L12907 | ⚠️ gap | shared | LangGraph, OpenAI text + vision, text-embedding-3-small and AgentRun tracking match. The ModelRouter can't pick a second model: get_model_credentials has no escalation purpose. AI team to confirm 3.13/Azure. | VA §4.4; CANON.md:119; STATE OD 9; ENV.md:91-93 |
| 205 | [182] PostgreSQL · L12921 | 🔁 aligned-changed | BE | Managed Postgres with pgvector, backups and encryption at rest; PG17 and psycopg 3 replace PG16 and asyncpg. | ADR-0003, ADR-0004 |
| 206 | [183] pgvector · L12935 | 🔁 aligned-changed | BE | HNSW (m=16, ef_construction=64), the plan's own choice, replaces the spec's IVFFlat; 1536-dim cosine. | Canon 0002b; ADR-0003 |
| 207 | [184] Redis · L12953 | 🔁 aligned-changed | BE | No Redis: queue, OAuth state and rate limits move to Postgres; no caches in v1 (spike-gated). | ADR-0004 §8; ADR-0005 |
| 208 | [185] ARQ · L12967 | 🔁 aligned-changed | BE | procrastinate replaces ARQ (maintenance-only) for generation, publish, image and import jobs. | ADR-0004 |
| 209 | [186] S3 · L12981 | ⚠️ gap | BE | GCS via the S3-API StorageAdapter replaces S3. Tenant key convention and processed-image versions unspecified (G12). | G12; ADR-0001 |
| 210 | [187] AWS Infrastructure · L12995 | 🔁 aligned-changed | BE | GCP replaces AWS, and ADR-0001 rules the network: Cloud Run's built-in Cloud SQL connection, with no VPC connector or private subnet. Tell the client (TEAM-QUESTIONS T5). | C7; ADR-0001 |
| 211 | [188] Deployment Architecture · L13009 | 🔁 aligned-changed | BE | Cloud Run revisions with zero-traffic smoke test, auto-rollback and tagged releases replace EC2 + git pull. | ADR-0001; STATE release flow |
| 212 | [189] Environment Strategy · L13023 | ❓ needs-client | client | Local/staging/prod match (local without Redis). What staging may publish to, and a Flipkart test account, are Q16. | Q16; ADR-0004 |
| 213 | [190] Local Development · L13059 | 🔁 aligned-changed | BE | Docker Compose kept with the PG17 image and no Redis; MinIO, uvicorn --reload and a gitignored .env planned. | ADR-0003, ADR-0004; CANON.md:64-66 |
| 214 | [191] Staging · L13073 | ❓ needs-client | client | Staging exists and auto-deploys from main. Its test marketplace accounts are Q16, and replacing UAT with automated suites awaits the team (C8, Q15, TEAM-QUESTIONS T8). | C8; Q15, Q16; STATE Decided |
| 215 | [192] MVP Operating Cost · L13087 | ❌ conflict | client | Spec: ~₹6,700 infra, ₹8-11k with LLM. ADR-0001: ~$195-205/mo (~₹16.5-17.5k) for prod + staging (C7); the ceiling is Q13. | C7; Q13; ADR-0001 |
| 216 | [193] Infrastructure Cost · L13145 | ❌ conflict | BE | Spec prices AWS at ~₹6,700/mo; ADR-0001's GCP is ~2.5× that, with no Redis. The client sets the ceiling (C7, Q13). | C7; Q13; ADR-0001, 0004 |
| 217 | [194] LLM Cost · L13159 | ✅ aligned | shared | Estimates tracked in agent_runs.cost_usd under USD budgets. 'Marketplace APIs are free' is questioned at [197]. | ENV.md:97-98 |
| 218 | [195] Embedding Cost · L13173 | ✅ aligned | BE | text-embedding-3-small; content_hash dedupe; cost in agent_runs and budgets. Approve→embed (G13) would add recurring cost. | Canon D12; DB §5.5; G13 |
| 219 | [196] Storage Cost · L13187 | ⚠️ gap | ops | GCS sits inside the ADR-0001 total. No egress line; lifecycle covers CSVs only, at 30 days (ADR-0001) vs 90 (DB §9); image archiving is G12. | ADR-0001; DB §9; G12 |
| 220 | [197] Marketplace API Cost · L13201 | ❓ needs-client | client | We assume free (ARCHITECTURE.md:104). Verify Amazon's 2026 SP-API developer fees; they depend on whose account files. | new; BLOCKERS.md:25 |
| 221 | [198] Scaling Cost · L13215 | ❌ conflict | ops | LLM tracking and budgets covered. Infra alone ~₹16.5-17.5k/mo likely breaks ₹10-20k at 50 brands with LLM (C7, Q13). | C7; Q13; ADR-0001 |
| 222 | [199] Cost per Listing · L13229 | ❓ needs-client | client | Formula is computable from agent_runs. The source gives ₹3-8 here and < ₹5 in [153]; the client picks the target. | new; VA §3; BUILD-SEQUENCE S14 |
| 223 | [200] Cost Optimization Strategy · L13243 | ⚠️ gap | shared | Routing, right-sizing, CUDs, embedding dedupe covered. No skip-if-unchanged generation or batch API; batch contracts are G10. | G10; new; ADR-0001 |
| 224 | [201] MVP Phase 1 · L13262 | ⚠️ gap | shared | Phase-1 list matches. Per-field publish results are only Proposed (G3). 'Basic analytics dashboard' is operational metrics by default (T14); whether it includes performance is Q10. | G3; C9; Q10; T14 |
| 225 | [202] Phase 2 · L13283 | ⏭ later-phase | shared | Version compare, prompt A/B, unified analytics, sales attribution and webhooks: Phase 2 in our plan too. | VA §4.14 |
| 226 | [203] Phase 3 · L13303 | ⏭ later-phase | shared | Auto-publish after a track record and the Optimization Agent are Phase 3; auto_publish is reserved OFF in v1. | C1; Q1; STATE Decided |
| 227 | [204] Future Marketplace Expansion · L13321 | ⏭ later-phase | BE | New marketplaces as adapters plus rules and mapping data; in practice a CHECK ALTER + enum MINOR. | VA §4.8; catalogus move 7 |
| 228 | [205] Future Agent Expansion · L13335 | ⏭ later-phase | AI | AgentInterface and registration are future; today there is one pipeline entry point. | CANON.md:109 |
| 229 | [206] Auto-Publish Evolution · L13349 | ⏭ later-phase | shared | Phase 3. v1 keeps approval with auto_publish OFF; track-record signals need review_decisions (G4) from v1. | C1, Q1, G4; DB §5.1 |
| 230 | [207] Optimization Evolution · L13363 | ⏭ later-phase | AI | Phase 3/4+ roadmap; needs review and performance signals captured in v1 (G4, C9). | PRD.md §7; G4, G14, C9 |
| 231 | [208] AI Learning Loop · L13377 | ⏭ later-phase | shared | Phase 3. Its training signal (approved examples, reject feedback, performance) must be captured from v1: G4, G13, G14. | G4, G13, G14 |
| 232 | [209] Production Readiness Checklist · L13391 | ✅ aligned | shared | Items map onto our gates and launch checklist (migrations, auth, OAuth, image pipeline, errors). | BUILD-SEQUENCE; DELEGATE-GATE.md |
| 233 | [210] Development Plan · L13414 | ⚠️ gap | shared | S1-S14 re-sequences steps 1-7 with Done-when tests; FastAPI JWT; observability moved early. Step 8 (brand vectorization) and steps 17-22 have no S-step yet (Q25). | BUILD-SEQUENCE; Canon D6; Q25 |
| 234 | [211] Module Dependency Graph · L13449 | ⚠️ gap | BE | Foundation order matches. No step for brand vectorization; adapters, publish, analytics, hardening unsequenced after S14. | new; G11; Q25; C9 |
| 235 | [212] Build Order · L13487 | ⚠️ gap | shared | S1-S14 re-sequences the foundation with Done-when tests, but nothing is sequenced after S14 and the schedule is behind (Q25). | Q25; C9; new |
| 236 | [213] Engineering Tasks · L13501 | ✅ aligned | shared | BUILD-SEQUENCE plus 202 per-area CHECKLISTS items, run test-first. We are behind the day plan (Q25). | Q25; CHECKLISTS.md |
| 237 | [214] Backend Checklist · L13515 | ⚠️ gap | BE | Redis/ARQ items become procrastinate (ADR-0004). 'All REST API endpoints' includes the unplanned error and agent-history APIs ([113], [114]); adapters are unsequenced after S14 (Q25). | ADR-0004; VA §4.9, §4.10; Q25 |
| 238 | [215] Frontend Checklist · L13538 | 🧩 other-team | FE | FE scope. Seams: FastAPI JWT, /api/v1, ErrorReport, ImageReport. Open: analytics (C9), preview (G11), batch approve (G10). | Canon D6; C3, C9; G10, G11, G19 |
| 239 | [216] AI Engineering Checklist · L13561 | ⚠️ gap | shared | AI team's list; brand chunking/embedding and the Pillow checks moved to the backend (CANON.md:129). 'Model routing logic' needs an escalation purpose in get_model_credentials. | CANON.md:119, :129; VA §4.4 |
| 240 | [217] DevOps Checklist · L13585 | 🔁 aligned-changed | BE | Items planned on GCP (ADR-0001) without managed Redis (ADR-0004); provisioning through OpenTofu in S4/S12. | ADR-0001, 0004 |
| 241 | [218] QA Checklist · L13608 | ⚠️ gap | BE | ADR-0010 sets the gates, quality bars and toolset; unit, rules, image, adapter, tenant and load tests are planned. Failure injection (network, model timeout, DB) is not in it. | ADR-0010; VA §4.13 |
| 242 | [219] Security Checklist · L13631 | ⚠️ gap | BE | All items planned; GCP Secret Manager replaces AWS. 'TLS on all connections' is stated nowhere for DB and storage ([162]). | ADR-0001; VA §4.11; Canon 0004/0005 |
| 243 | [220] Production Launch Checklist · L13655 | ⚠️ gap | shared | Launch items map to our gates; rollback is ADR-0009. Prod OAuth depends on filings (Q12); no incident response plan or ops owner (Q18). | Q12, Q18; ADR-0009 |
| 244 | [221] Architectural Invariants · L13678 | ✅ aligned | shared | The client's 14 invariants map onto our 10 (4+5→4, 11+12→10); #13 is Phase 3, #14 a design principle. | RULES.md §1; VA §4.14 |

## What must change in our plan

Every gap, conflict and needs-client page above appears here, duplicates merged, most urgent first.
Nothing is applied yet: each item needs the user's OK plus an ADR or a Canon PR (`docs/PROCESS.md` §2).

### A. Backend + DB foundation: before migrations 0001-0003 and the `schemas-v1.0.0` tag

Once migrations 0001-0003 and the tag land, changing any of these is a MAJOR three-team change. The
Tier A gaps (G1-G8, G10, G15; the user accepts them through Q24) come first, with the conflict rulings
their shape depends on (C3, C4, C5).

1. **Attribute provenance (G1).** An envelope per attribute (value, normalized, source, confidence,
   evidence), per-category critical fields whose source can't be AI, and canonical attribute keys.
   *Lands in:* 0001. *Pages:* B★, B03, B04, B05, [20], [21], [23], [65], [89].
2. **Compliance, commercial and identifier fields (G2).** A brand compliance profile; MRP, price, HSN,
   origin, identifier or exemption, package and size data per product or variant; a contract passing
   these facts to adapters (Flipkart needs them). *Lands in:* 0001. *Pages:* B★, B02, B03, B04, B08,
   [88].
3. **Publish ledger and LIVE confirmation (G3, C2).** `external_id` in the Canon DDL,
   `publish_field_results`, `listing_submissions`, marketplace state columns (SKU, live URL, confirmed
   and synced times) and a bounded status-sync job; retries read marketplace state first. *Lands in:*
   0002/0003. *Pages:* B★, B01, B04, B05, P01, P12, [13], [85], [91], [136], [137], [201].
4. **Status model (C3, G16).** Keep 9 statuses; add `paused_at`, `archived_at`, a derived
   `human_action_required`, a BRD-named display projection and the `listings_status_guard` transitions.
   *Lands in:* 0002 and S2. *Pages:* B★, B06, P07, [17], [91].
5. **CanonicalListing shape (C5).** A Canon row for nested content, `hidden_terms` and `images[]`, plus
   a description markup contract (new) and `locale` (G15). *Lands in:* `schemas-v1.0.0`. *Pages:* B10,
   P06, [27], [34], [35], [38], [39], [42], [65].
6. **Intake template (C4).** An ADR before S13: named CSV columns for the BRD core and G2 fields, JSON
   for the long tail; the §21 walkthrough becomes the E2E fixture. *Pages:* B★, B09.
7. **Rule types (G6).** `allowed_values`, `conditional_required`, `cross_field`, `pattern`, `min_items`,
   `claim_requires_attribute`, image thresholds per slot and marketplace, `policy_ref`; fix the rules
   unique key. *Lands in:* `schemas-v1.0.0`. *Pages:* B08, P03, P09, [11], [21], [23], [26], [27], [29],
   [36], [37], [39], [40], [42], [44], [45], [46], [47], [49], [51], [61], [62], [71], [89], [119],
   [166].
8. **Versioned category templates (G7).** Version, source, `fetched_at`, `product_type`, per-attribute
   `{marketplace_field, value_map}`; store the template version beside `rules_version`. *Lands in:*
   0003. *Pages:* B01, B05, [40], [65], [72].
9. **Variant model (G8).** A colour level, the full size×colour matrix, listing per child or per family.
   ADR-0007 already settles the parent/child family and its publishing. *Lands in:* before 0002.
   *Pages:* B03.
10. **Review decisions (G4).** ADR-0008 makes every approval write a `review_decisions` row. Still
    missing: a reject decision with reason and feedback in the approve contract (or a reject
    endpoint), `listing_versions.origin` and an optional `review_feedback`; also the Phase-3 training
    signal. *Lands in:* 0002 and `schemas-v1.0.0`. *Pages:* P01, P07, P12, [7], [12], [17], [97],
    [108], [124].
11. **Issues and human tasks (G5).** `listing_issues` and `human_tasks` for "ask one question, then
    resume". *Lands in:* 0002. *Pages:* B★, B01, B04, B07.
12. **Batch contracts (G10).** Batch generate, approve and publish with a result per item, plus
    `job_runs.batch_id`. *Lands in:* `schemas-v1.0.0`. *Pages:* [7], [103], [105], [140], [200].
13. **Image data (G12, new).** Verdicts per (image, marketplace), tenant-prefixed keys; rule on
    formats (JPEG/PNG/TIFF by header vs `.webp`), images per CSV row and EXIF derivatives. *Lands in:*
    0002 and the S13 row model. *Pages:* [8], [29], [45], [46], [52], [186].
14. **Brand chunks and profile (G13, new).** `type` CHECK, `weight`, `updated_at`, approve →
    `brand_embed`, and the chunk ids that fed each version (`tenants.guidelines` is already JSONB in
    DB §5.1). *Lands in:* 0001/0002. *Pages:* P06, P08, [30], [53], [54], [55], [59], [60], [61],
    [85], [98].
15. **Approve precondition (G9).** Product facts into `validate()`, so claims are checked against the
    product record; approve and publish assert a passed validation of that exact version (DB §7.1
    drafts the approve side). *Lands in:* S2, with Done-when tests in S10/S14. *Pages:* [2], [21],
    [23], [26], [34].
16. **Account settings and reconnect (G19, new).** `settings`, `needs_reconnect`, and one refresh count:
    the spec and S13 escalate after one failed refresh, while the DB draft's Proposed
    `reauth_required` waits for three. *Lands in:* 0002. *Pages:* B01, [7], [9], [25], [132].
17. **Audit (G11, audit part).** Add `audit_log.reason` and a credential-use action; let platform rule
    changes log with a NULL tenant (KNOWN-ISSUES B). *Lands in:* 0002. *Pages:* [97], [167].

### B. Later backend steps (additive; each ships with its build step)

1. **Adapter ABC (G11):** frozen with `get_performance` and a network-free `preview()`, around Day 29.
   *Pages:* P09, [12], [66], [124].
2. **Outbound limiter (G17):** a Postgres token bucket per marketplace, account and operation. *Pages:*
   [2], [7], [66], [67], [130], [134], [135], [141].
3. **Error taxonomy as data (G18):** raw marketplace code → code, class and auto-fixable, plus the
   4-class retry map. *Pages:* B★, B01, B07.
4. **Publish ADR:** pin the backoff numbers and the partial-field retry cap of 3, and read the
   marketplace state before any retry. *Pages:* [132], [133], [136].
5. **Brand-document S-step:** the parser, formats, size cap and JobKind; per-clause, per-term and
   per-field splitting; re-embedding on update; batch vs incremental. Decide before the Week-3 RAG work.
   *Pages:* P03, P08, [54], [55], [56], [82], [210], [211].
6. **Sequence after S14** (adapters, publish, analytics, hardening), then re-baseline the schedule
   (Q25). *Pages:* P01, P03, P04, P12, [82], [210], [211], [212], [214].
7. **Error and job APIs:** `GET /api/v1/errors` with filters and an index, plus job history and cancel.
   *Pages:* [113], [114], [214].
8. **Rules write path:** today platform rules change only through a release, and brand-defined rules
   have no write path (the DB draft even proposes none in v1). *Pages:* [62], [70], [93], [119].
9. **Tracing:** carry `traceparent` in job payloads, derive agent spans from `emit_node_event`, and drop
   S11's ARQ/Redis Sentry items. *Pages:* [170], [175], [176].
10. **Security docs:** state TLS for DB and storage (ADR-0001/S4); tell the client that env-var secrets
    rotate when a new revision starts ([161]). *Pages:* P10, [154], [162], [219].
11. **Testing:** add failure injection (network, model timeout, DB, token expiry, partial publish) to
    ADR-0010's suites; rehearse a restore in under 15 min (P11 gate 6) against the DB draft's
    RTO ≤ 2 h. Rollback itself is ADR-0009. *Pages:* P03, P11, [218].
12. **Replay semantics (Q26):** the same request with a fresh context. *Pages:* [19], [131], [138],
    [139].
13. **Cost levers:** skip-if-unchanged generation keyed on `input_digest`, per-agent and per-marketplace
    budgets (Q19), a GCS egress line, and CSV retention (30 vs 90 days). *Pages:* [148], [149], [196],
    [200].
14. **Performance ingestion (G14):** only if C9 keeps it in v1. *Pages:* [14], [96].

### C. Other teams (our side: the seam, contracts, and a Canon PR where noted)

- **AI team:**
  - An escalation purpose in `get_model_credentials`, through an AI-seam Canon PR with us. *Pages:* P08,
    [22], [25], [142], [143], [144], [145], [147], [181], [216].
  - A relevance threshold on `similarity`. *Pages:* [57], [59].
  - The confidence-formula ADR by Day 21. *Pages:* [1], [127], [128].
  - Claim grounding, the title pattern and keyword distinctness in prompts. *Pages:* [21], [26], [27],
    [37], [39].
  - Define the SEO score or drop it. *Pages:* [33].
  - Fill `prompt_version` from day one. *Pages:* [150].
  - Confirm Python 3.13, LangGraph and Azure OpenAI. *Pages:* [178], [181].
- **FE team:**
  - The BRD-named status projection. *Pages:* B06.
  - An approval screen with the preview, reject-with-feedback and batch approve. *Pages:* [12], [124].
  - NextAuth only as a wrapper around our tokens. *Pages:* [116].
  - Error paths from `paths.py`. *Pages:* [11].
  - Analytics once C9 is settled. *Pages:* [111], [215].

### D. Client decisions (defaults in VISION-ALIGNMENT §7; ordered by what they block)

1. **Autonomy and unlock granularity** (Q1, C1), before 0001. *Pages:* B★, B01, [203], [206].
2. **Brands behind one seller account** (Q6), before 0001. *Pages:* B02, [88].
3. **What "listed" means, and the status names** (Q2, Q3; C2, C3). *Pages:* B★, B01, B06.
4. **Legal Metrology blocking set, and the AI inference policy** (Q8, Q9). *Pages:* B08, B10.
5. **Launch categories** (Q7). *Pages:* [36], [71].
6. **Scope additions and stack deltas** (Q14, C6). *Pages:* P02, [178].
7. **v1 analytics, and whether performance data is v1** (Q10, C9). Operational metrics follow
   TEAM-QUESTIONS T14's default; the open part is marketplace performance in MVP, where the site
   contradicts itself (L6880 vs L8189, L11131). Settle the SP-API roles before filing. *Pages:* [1],
   [5], [6], [14], [96], [111], [201].
8. **Import auto-generate** (Q11). The site is consistent that valid rows fan out into generation
   (L7806, L7929, L11249, L11345, L12075), so VA's default 'no' departs from a clear statement and
   belongs in TEAM-QUESTIONS; ask only which marketplaces and what cost cap. *Pages:* [103], [105],
   [140].
9. **Auto-fix resubmission without re-approval** (Q4). *Pages:* B★, B01, B07.
10. **One-person brands** (Q5, C10; TEAM-QUESTIONS T15). *Pages:* P03, [4].
11. **Filings and prod OAuth** (Q12), plus Amazon's 2026 SP-API developer fees (new). *Pages:* P03,
    [197], [220].
12. **Year-2 infrastructure ceiling** (Q13, C7). The DB network path is ADR-0001's ruling; tell the
    client (TEAM-QUESTIONS T5; P05, [187]). *Pages:* [192], [193], [198].
13. **UAT, and what staging may publish to** (Q15, C8, Q16). *Pages:* P04, P11, [189], [191].
14. **Role rights, ops owner, budget-cap behaviour, catalog sync, social login and AI-provider
    retention** (Q17-Q22). *Pages:* P07, [1], [3], [4], [6], [117], [138], [142], [148], [157],
    [165], [220].
15. **New:** the cost-per-listing target (₹3-8 or under ₹5); append-only audit vs DPDP erasure; the
    severity of Flipkart's "restricted" text. *Pages:* [51], [126], [199].
