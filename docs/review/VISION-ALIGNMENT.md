# VISION ALIGNMENT: the client's vision against our direction

_Written 2026-09-27 (Day 7). **Status: traceability record.** It changes no rule; every action here is
a proposal that needs the user's OK, an ADR or a client answer (AGENTS.md §6)._

- **Client source:** `docs/source/system-arch-llms-full.txt`, a dump of https://system-arch.vercel.app
  (fetched 2026-09-26, unchanged 2026-09-27). `L<n>` = line in that file; these are stable.
- **Our side:** AGENTS.md, docs/STATE.md, CANON.md + CANON-ADDENDUM.md (incl. the ADR-0001..0005
  amendments of 2026-09-27), docs/adr/, KNOWN-ISSUES.md and
  docs/research/{catalogus,stack-2026-09,hosting-2026-09}.md.
  Our line numbers are as of 2026-09-27 and drift; the row or section name is the anchor.
  `docs/architecture/DATABASE.md` ("DB §n") landed while this was written; §6.1 notes what it
  already proposes.
- **Status:** `aligned` = we do what the client wrote; `superseded` = a Canon/STATE/ADR ruling
  deliberately differs (just tell the client); `gap` = asked for, not planned; `conflict` = disagreement
  with no ruling; `client` = only a client answer settles it.
- **IDs:** C = conflict (§5), G = gap (§6), Q = question (§7).

## 1. Summary

We are closely aligned with the **team-side** material (the 45-day build plan and specs [1]-[221]),
because the Canon was distilled from it: same invariants, safety chain, tenancy, ErrorReport,
maker-checker, adapters, non-goals and scope lock. Where we differ, it is a deliberate ruling the
client only needs to hear about: `tenant_id`, `/api/v1`, 9 persisted listing statuses (the plan's own
DDL set, L5937, not its 14-stage diagram), FastAPI as JWT issuer and, since 2026-09-27, Python 3.13,
PG 17 and a Postgres job queue with no Redis (ADRs 0002-0005). HNSW is the plan's choice too
(L5813, L5906); only spec [183] says IVFFlat (L12945). Hosting on GCP asia-south1 (ADR-0001)
matches the build plan.
Alignment breaks on the **Founder BRD** (L10-2286), which the Canon never read (KNOWN-ISSUES.md:12).
It describes a more ambitious product: an autonomous, apparel-first listing **operations** agent
that owns a listing until the marketplace confirms it LIVE, with per-attribute provenance, Legal
Metrology data, versioned marketplace schemas, first-class Issues and Human Tasks, and 14 statuses.
The STATE decisions of 2026-09-27 (human approval in v1, apparel variant families, memberships) close
part of the gap. Most of the rest is schema and contract shape: cheap before migrations 0001-0003 and
the `schemas-v1.0.0` tag, a MAJOR three-team change after.

**The eight that matter most:**
1. **Autonomy (C1).** BRD: autonomous submission by default (L2243). Build plan and specs: zero
   auto-publish in Phase 1 (L4765-4769, L7483, L11786-11788). v1 is settled; the long term is not.
2. **"Published" is not "LIVE" (C2, G3).** The BRD counts a listing only when the marketplace confirms
   it (L183-185, L1079-1087). We set `published` from the submit call, with no status sync and no
   columns for ASIN/FSN, live URL or last sync.
3. **Status model (C3).** BRD 14 vs our 9. PROCESSING and LIVE collapse into `published`;
   PARTIALLY_LIVE, HUMAN_ACTION_REQUIRED, PAUSED and ARCHIVED have no home. (REJECTED and
   PARTIALLY_PUBLISHED are spec [17]'s, L8609/L8613, not the BRD's.) STATE and KNOWN-ISSUES wrongly
   say "16-state"; the site's own page [06] says 14 (L3417).
4. **Provenance and compliance (G1, G2).** No per-attribute source or confidence; no Legal Metrology,
   commercial or identifier fields; no brand compliance profile. So the Rules Engine cannot stop
   AI-invented fabric, origin or MRP. The largest DB gap.
5. **Issues and Human Tasks (G5).** "Ask one question, then resume" has no table, endpoint or seam.
6. **Rules expressiveness (G6, G7).** 5 rule types; the client needs allowed-value, conditional,
   cross-field, pattern, claim and image-threshold rules, and versioned marketplace schemas.
7. **Publish ledger (G3, G4).** Nothing stores per-field or per-variant publish results, submission
   records or reject feedback. Catalogus moves 1-2 are still proposals.
8. **Delivery vs the written plan (C6, C7).** The plan expected GCP, CI, 0001/0002, auth and a staging
   skeleton by Day 7 (L4873-4968); we are pre-S1. Our stack departs from the client's written stack.
   ADR-0001's ~$195-205/month (prod + staging) fits the plan's $150-250 (L7061) but is ~2.5x the
   specs' ₹6,700 infra line (L13155), and it drops the plan's private-IP DB path (L5851-5855).

## 2. The client's vision in one page (Founder BRD, L10-2286)

- **What:** "a listing operations agent, not merely a content generator" (L59-61). The seller gives
  brand and product facts once; the agent runs the lifecycle "until the listing is live or a human
  decision is genuinely required" (L37-41). Never ask for what can be derived, reused or validated
  (L55-57).
- **Who:** apparel/fashion (L21-25, L43-45). D2C brands, manufacturers, traders, small sellers and
  marketplace agencies (L47-49).
- **Flow** (19 steps L63-179; 14 workflow steps L943-1087; chain L2283): onboard brand + marketplace
  accounts once (L245-387) -> form/spreadsheet intake, merged -> understand (facts vs marketing) ->
  classify per marketplace with confidence -> extract and normalize attributes (seller value,
  normalized value, source, confidence) -> one canonical product record with a style/colour/size
  family -> map to the current versioned marketplace schema -> generate from verified facts only
  (Golden Rule, L757-765) -> 9 quality gates (L1388-1428) incl. Legal Metrology 2022 (L1430-1508) ->
  recover missing data in strict order, ask one targeted question only if still blocked -> submit and
  record the exact version -> poll to a terminal state -> plain-language errors -> auto-fix
  deterministic classes and resubmit under anti-loop rules -> LIVE only on marketplace confirmation,
  with ASIN/FSN and URL -> per-marketplace dashboard and audit trail.
- **Marketplaces:** up to six (Amazon, Flipkart, Myntra, AJIO, Meesho, Nykaa Fashion); start with 2-3
  (L219-243, L2233-2235).
- **v1 must:** meet 17 acceptance criteria (L2146-2164). The §21 walkthrough (one T-shirt, four sizes,
  L2084-2144) is the de-facto UAT script.
- **v1 metrics:** 10 (L2166-2210), e.g. time to first listing, first-pass success, auto-resolution,
  questions per SKU, live-confirmation accuracy.
- **Not v1** (L181-217): orders, inventory, ads, pricing optimization, returns, customer service, a
  "whole marketplace OS". Image generation is never a v1 dependency (L2249-2251).
- **BRD open decisions** (§25, L2227-2267), with its own recommendations: 2-3 marketplaces;
  form + spreadsheet first; **autonomous submission with optional approval**; English first,
  language-ready; no image generation; versioned schemas; an internal error taxonomy; an inference
  policy; single and bulk jobs through one workflow.

## 3. Source map

| Part | Lines | Pages | What it is | Relation to our Canon |
|---|---|---|---|---|
| Complete Founder BRD | L10-2286 | 1 | Founder's V1 business requirements, apparel (L14) | **Not a Canon input.** Most conflicts and gaps come from here. |
| BRD split pages [01]-[10] | L2287-4640 | 10 | Same body as the Complete BRD (diffed, blank lines ignored) | Cite the Complete page. Page descriptions overstate: [09] "40+ columns" (64), [04] "swatch shot rules" (none), [02] "credentials" (none). |
| 45-Day Build Plan "GCP Edition" [01]-[12] | L4641-7064 | 12 | Team-side delivery plan: DoD, invariants, day plan, DDL, state machine, API, launch gate, costs | **Primary Canon input.** Translates the original AWS design to GCP (L5801-5855). |
| Specs [1]-[221] | L7065-13711 | 221 | Team-side reference architecture, 19 domains; infra pages are AWS | **Primary Canon input.** Some parts overruled by name, e.g. spec [16] nodes (CANON.md:124). |

**The client material contradicts itself** (each needs one ruling; C1, C5 and C10 too):
- Status models: BRD 14 (L1101-1193); plan [07] 14 *different* stages (L6107-6134); spec [17] 17
  (L8590-8618); the plan DDL 9 (L5937, our set); the spec DDL 5 (L10943), while spec [91] says
  "state machine value" (L11061). None has 16.
- Cloud: AWS in specs (L12867-12869, L13005) vs GCP in plan (L5717, L5801-5855).
- Cost: ₹6,700/month infra (L13155) vs $150-250/month (L7061). Cost per listing ₹3-8 (L13239) vs
  < ₹5 (L12356-12360).
- Catalog sync: MVP (L7392, L7200) vs Future (L7902-7906) and deferred by the plan (L4745).
- Analytics and performance: the plan's Phase-1 analytics is operational only (volume, approval,
  validation pass, publish success; L4773, L5370, L6954-6956) and puts "automated performance
  tracking" in Phase 2 (L6880; also L13297). The specs say performance is collected on a schedule
  and shown in MVP analytics (L8189, L11131), and define `GET /analytics` as "unified performance +
  sales attribution" (L11285, L11447), while sales attribution is Phase 2 (L4775, L7557, L13296).
- Maker-checker scope: the plan bars whoever "generates or edits" (L6620); specs [4] and [12] bar only
  the generator (L7481, L8131).
- Vector index: HNSW in the plan (L5813, L5906, L6039), IVFFlat in spec [183] (L12945), either in
  spec [101] (L11204).
- Variants exist only in the BRD: the plan and specs never model size/colour families.
- Swatch images in the BRD (L121-125) and Day 22 (L5160-5162), forbidden by the Canon (CANON.md:59).

**Glossary trap:** the BRD's "canonical product record" (L767-825) is our `products` row plus its
variants. Our `CanonicalListing` is per-marketplace *content* with neutral field names.

## 4. Alignment matrix by theme

Plainly aligned items are summarized under each table.

### 4.1 Product scope and flow

| Requirement (source) | Status | Our side | Action |
|---|---|---|---|
| Own the listing until the marketplace confirms it live (L37-41, L61, L2283) | conflict | We stop at the submit result (AGENTS §1; CONTRACTS.md:623-650) | C2, G3 |
| Understand, classify, detect conflicts, find missing data (L943-1037) | gap | 12 PipelineStage nodes, none for this (CANON.md:54) | AI team; backend stores conflicts as issues (G5) |
| Phase-1 "left-column rule" (L4733, L4801-4805) | client | STATE adds variant families, memberships, tenant lifecycle, `auto_publish` | Q14 |
| Basic operational analytics in Phase 1: DoD, scope table, Day 39, `GET /analytics`; launch gate 1 covers the left column (L4690, L4771-4773, L5368-5373, L6754, L6952-6956, L7545-7547, L13279) | conflict | ARCHITECTURE.md:69 puts approval rate etc. in Phase 2 (that list is spec [153], Phase 2, L12316-12360); S3 has 501 stubs only (BUILD-SEQUENCE.md:48) | C9 |
| Performance collected on a schedule (L8179-8189, L11131); but the plan defers "automated performance tracking" to Phase 2 (L6880, L13297) | client | Table only; no job or adapter method | G14, C9, Q10 |
| Catalog sync: MVP (L7392) vs Future (L7902-7906) | client | Out of MVP (ARCHITECTURE.md:70) | Q20 |

Aligned: the safety chain and 4 layers (L4709-4715, L7103-7186); single and bulk through one workflow
(L2265-2267), except bulk *generation* (G10).

### 4.2 Users, roles and onboarding

| Requirement (source) | Status | Our side | Action |
|---|---|---|---|
| Brand and Seller Account separate; several brands per business (L263-267, L837-847) | client | tenant == brand (CANON.md:47), as in the plan and specs (L5874, L11005) | Q6, before 0001 |
| One-person operator still reviews (L7483); Day-45 DoD: "a real brand user can independently ... generate, approve, and publish" (L5450) | conflict | Maker-checker: a one-login brand can never publish. The same DoD also says the creator cannot approve (L4689) | C10, Q5 |
| Brand registry / authorization and category permissions per marketplace; permission errors become a human task (L293-297, L361-363, L963, L1326-1330) | gap | No field; no permission error code | G19, G18, G5 |
| Four roles with spec [4] rights (L7445-7475) | superseded | Canon D13 defaults (CANON.md:152): reviewer may regenerate, operator may publish, editor can't approve | Q17 (never confirmed) |
| Email/password **and** OAuth logins (L12484) | client | Email + password in S10 | Q21 |
| JWT minted by NextAuth (L11517, L12380, L4948) | superseded | FastAPI sole issuer (CANON.md:152) | Tell FE and client |
| An operator clears SKU conflicts, BLOCKED, DLQ (L6250-6262, L12045-12047) | client | Ops role undefined (KNOWN-ISSUES.md:17) | Q18 |

Aligned: agencies (L47-49; memberships, STATE:70-75); sign in and pick a brand (L4683; `tid` = active
tenant); generator != approver (L7477-7481, L8131); the plan also bars editors (L6620), as we do
(CANON.md:97). The BRD is silent on onboarding mode; invite-only (STATE:76-78) fits. The source says
nothing about product pricing, plans or billing, so `tenants.plan` (Q19) has no client input yet.

### 4.3 Catalog and apparel variants

| Requirement (source) | Status | Our side | Action |
|---|---|---|---|
| Style -> colour -> size; full matrix incl. unavailable combos; never invent a variant; stable seller SKU (L603-637) | gap | Two levels in STATE:66-69 | G8 |
| Parent/child recorded apart from marketplace variation rules (L638, L1556-1558) | aligned (proposed) | DB §5.2: `variation_theme` is a canonical slug that adapters map | — |
| Status per marketplace **and per variant** (L903-907, L2219) | gap | listings `UNIQUE (product_id, marketplace)` | G8, before 0002 |
| 64-column Master Field Dictionary as the template (L1560-2082) | conflict | CSV `sku,title,category` + JSON `attributes` (scaffold row_model.py:11-42) | C4 |
| Keep seller and normalized values, e.g. "Jet Black" (L477-483, L971-975) | gap | Flat string map | G1 |
| Manufacturer, style no., product type, sub-category, GTIN type or exemption (L389-597) | gap | One `gtin` string | G2 |
| Canonical attribute dictionary per sub-category (L389-597) | gap | `category_templates` is per marketplace (CANON.md:96) | G7 |
| Merge inputs, don't re-ask (L373-387) | gap | Idempotent by SKU only | G1 source priority |
| Every CSV row has SKU, title, >= 1 image (L7927-7929) | conflict | Scaffold: images optional, accepts .webp (spec: JPEG/PNG/TIFF, L9920-9984) | Fix in the S13 row model |
| Media roles: detail/back, colour-specific, packaging/label evidence, file name (L641-691) | gap | ImageSlot main/secondary/lifestyle/size_chart (CANON.md:59) | G12 |
| Swatch shots (L121-125, L5160-5162) | superseded | Forbidden (CANON.md:59) | Q23 |

Aligned: apparel with first-class variant families (L21-25, L2218; STATE:66-69; ADR-0007 to write);
form + spreadsheet intake in v1 (L2237-2239).

### 4.4 Content, AI and brand voice

| Requirement (source) | Status | Our side | Action |
|---|---|---|---|
| CanonicalListing: flat content + `images[]` in [06] vs nested `content` + `category/status/image_reports/version` in [35]; both carry `keywords.backend_keywords` (L6051-6094, L9643-9673) | conflict | Nested + `hidden_terms` + `images[]` (DECISIONS D3); no Canon row | C5 |
| Golden Rule: never invent composition, measurements, certifications, care, origin, tax (L757-765, L987-997, L8830-8843) | gap | `UNSUPPORTED_CLAIM` exists (CONTRACTS.md:173); no rule emits it | G1 + G6 |
| Rules Engine enforces glossary and brand compliance (L10412-10428) | gap | Tenant rules in DDL; no write path; no claim rule | G6; glossary -> tenant `banned_terms` |
| Approved listings become brand memory, weighted (L10398-10400) | gap | No approve -> `brand_embed` trigger; no weight | G13 |
| Chunk types tone_rule/glossary/compliance/example (L5895-5906, L11159) | gap | `type` is free text | G13 |
| Reject feedback feeds generation (L8125-8127) | gap | Not stored | G4 |
| English first, language-ready (L2245-2247) | gap | No locale field | G15 |
| Confidence score shown to reviewer (L5312-5314, L11832-11855) | gap | Formula owed by AI team (KNOWN-ISSUES.md:17) | AI ADR; store formula version |
| Brand guide upload, chunk, batch embed (L5070-5088); the plan gives chunking to the AI engineer (L5083) | gap | Backend owns chunking (CANON.md:129), a deliberate move; no parser/format/cap/JobKind | Decide before Week-3 RAG; tell the AI lead |
| Escalate to gpt-4o after 2 JSON failures (L8873-8879, L12101-12156) | gap | `get_model_credentials` has no escalation purpose | AI-seam Canon PR |

Aligned: content objects (L693-761; CONTRACTS.md:258-280); RAG top-k 8 with text-embedding-3-small
(L10318-10333). DeepSeek is superseded: deferred (CANON.md:71).

### 4.5 Validation, quality gates and Legal Metrology

| Requirement (source) | Status | Our side | Action |
|---|---|---|---|
| Required/conditional/optional/unsupported; allowed values; MRP vs price; identifiers; HSN (L999-1007, L2153, L11561-11597) | gap | 5 RuleDef types (Addendum §16) | G6 |
| 9 quality gates (L1388-1428) | gap | `validate()` gets a CanonicalListing only (no price, MRP, HSN, identifiers, family) | G9 + G6 |
| Versioned, refreshable schemas: Amazon PTD, Flipkart verticals (L224-228, L855-859, L2253-2255) | gap | `category_templates` unversioned | G7 |
| Legal Metrology 2022; origin never inferred; HSN seller-verified (L1430-1508) | gap | Nothing in the Canon DDL (catalogus.md:71-72 only aims to hold it as data) | G2 + Q8 |
| Declarations the 64-column template lacks: marketer/brand owner, importer, size declaration with metric measurements, package declarations; the general rules add generic name and net quantity (L1446-1456, L1470-1474, L1494-1498, L1508) | gap | None | G2, Q8 |
| Image rules as data: 1000x1000, white, fill >= 85%, aspect (L5184-5190, L9902-9908, L10135-10179) | gap | No image RuleDef; would be hardcoded | G6 |
| One image, a verdict per marketplace (L10019-10029, L5174, L11173) | gap | Single `compliance_status`; per-marketplace report only as a JSON cache (DB §5.2) | G12 |
| Approved version must itself have passed validation (L7323-7325, L8113-8117) | gap | No passed-validation precondition on approve | G9 |
| Accept image warnings and proceed (L10121-10123) | gap | No action or audit | G12 |

Aligned: deterministic rules as versioned data (L7323-7325, L11559; inv 2; Addendum §4), after fixing
the unique key and version rule (KNOWN-ISSUES.md:23-24); schema -> rules -> vision (L8024-8041);
PASS/BLOCKED with field reasons (L999-1007). "NEEDS HUMAN INPUT" is G5.

### 4.6 Status lifecycle

| Requirement (source) | Status | Our side | Action |
|---|---|---|---|
| 14 BRD statuses, DRAFT..ARCHIVED (L1101-1193) | conflict | 9 persisted lowercase (CANON.md:53) | C3 |
| LIVE only after confirmation (L1079-1087, L2162) | conflict | `published` = submit accepted | C2 |
| PARTIALLY_LIVE; overall product status (L1159-1163, L1243-1245) | gap | Per listing only | Derived view (G3) |
| PAUSED, ARCHIVED (L1183-1193) | gap | `products.deleted_at` only | C3 |
| HUMAN_ACTION_REQUIRED (L1177-1181) | gap | None | Derive from open human tasks (G5) |
| Explicit transitions, e.g. REJECTED -> GENERATING (L8603-8684) | gap (proposed) | DB §6.1 `listings_status_guard`; `generated` unmapped (KNOWN-ISSUES.md:27) | G16 |
| Spec [17] 17 states; plan [07] 14 stages (L8590-8618, L6107-6134) | superseded | 9 persisted + transient stages (CANON.md:53-54) | REJECTED/partial become data (G3, G4) |

### 4.7 Approval, human-in-the-loop and autonomy

| Requirement (source) | Status | Our side | Action |
|---|---|---|---|
| Autonomous by default, approval optional (L39-41, L1129-1133, L1362, L2243) | superseded for v1 | STATE:63-65; inv 3; `auto_publish` OFF | C1, Q1 |
| Phase-3 unlock by track record + confidence (L13315, L13359) | aligned | One boolean per tenant | Q1: granularity before the column lands |
| Reject with feedback, edit, regenerate (L8089-8127, L5312-5313) | gap | Reject has no state (KNOWN-ISSUES.md:15); regenerate missing from S3 list | G4 |
| Reviewer sees exactly what each marketplace gets (L8089-8091, L1255-1268) | gap | No adapter preview | G11 |
| One targeted question, then resume (L1027-1037, L1360-1386, L2156) | gap | Whole-listing review only | G5 |
| Deterministic auto-fix after rejection, resubmit; the §21 walkthrough resubmits with no re-approval (L157-167, L1059-1077, L2130-2136, L2159) | client | Approved content immutable (CANON.md:97) | Q4 |
| Per-row approval after a bulk import (L7427, L7922, L12075) | gap | Single-item contracts | G10 |

Aligned: anti-loop (L1332-1358; inv 9-10). Add "the payload hash must change unless the error class is
retryable".

### 4.8 Publishing and marketplaces

| Requirement (source) | Status | Our side | Action |
|---|---|---|---|
| Adapter ABC incl. get_performance, per-field ListingResult (L6497-6536, L10516-10533) | gap | ListingResult exists; ABC unwritten; FakeAdapter only (CANON.md:128) | G11 |
| Persist per-field results; retry only failed fields (L8161-8167, L12031-12033, L5276-5304) | gap | No table (catalogus move 1) | G3 |
| Submission record: time, reference, exact version, attempts, payload (L1039-1047, L1071-1077, L1267) | gap | `job_runs` + `raw_response_digest` | G3 |
| Poll until terminal, plus webhook listeners for feed outcomes (L145-149, L1049-1057) | gap | No status-sync JobKind; no notification listener (§6.2 "Later") | G3, polling first |
| ASIN/FSN, marketplace SKU, live URL, last sync (L169-173, L1079-1087) | gap | `external_id` is in both client DDLs (L5938, L10944) but no Canon row names it (DB §5.3 re-proposes it); the rest is missing | G3 |
| Marketplace dedupes on our key (L12015-12017) | gap | CHECKLISTS.md:254 assumes Amazon/Flipkart de-duplicate on our key; nothing verifies it | Own ledger + read-before-retry (G3) |
| Flipkart needs MRP, selling price, package dimensions/weight, HSN and fulfilment fields; Amazon needs identifiers or an exemption (L1520-1530) | gap | `mrp`/`price`/`weight_grams` proposed on products (DB §5.2); no HSN, package dims or fulfilment fields; CanonicalListing rightly carries none (inv 4), and no contract passes product facts to the adapter | G2 + an adapter input contract before the Flipkart adapter (Day 32-33, L5286-5304) |
| Rate limit per seller account and operation (L10559-10561, L11985-12001, L12089) | gap | Inbound limits, queue caps | G17 |
| Field/value maps as data: navy blue -> Navy / Navy Blue (L10633-10635) | gap | CategoryTemplate has required/optional only | G7 |
| 11 error classes, plain language, unknown -> stop + task (L1270-1354, L2257-2259); 4 retry classes (L11744-11774) | gap | `retryable: bool`; codes need the Tech Lead | G18 |
| Account profile: fulfilment/SLA, locations, approvals, GTIN exemption (L335-371) | gap | Auth fields + proposed `status` incl. `reauth_required` (DB §5.4) | G19 |
| Flipkart sandbox by Day 32 (L5286-5292) | client | Unconfirmed (CHECKLISTS.md:19, :61) | Q16 |

Aligned: Amazon + Flipkart only (L4747-4751, L7079-7083; the BRD's own "2-3"); broad canonical, narrow
adapters (L221, L10484-10502; inv 4); OAuth with auto-refresh, escalating on failure (L7967-7968,
L10547; "refresh once" in L11943-11947), which still needs `needs_reconnect` (G19). "No core
rewrite" for a new marketplace (L13331) means a CHECK ALTER plus an enum MINOR.

### 4.9 Data model and tenancy

| Requirement (source) | Status | Our side | Action |
|---|---|---|---|
| Per-attribute source/confidence/evidence; source priority; no AI guess on critical facts (L765, L867-871, L933-941, L2216) | gap | Plain JSONB `products.attributes` (DB §5.2); `AttributeValue` is a bare scalar (CONTRACTS.md:245) | **G1** |
| Canonical record with dimensions, compliance, classification confidence, mappings, issues (L767-825) | gap | Those parts missing | G1-G5 |
| 16 logical records incl. Product Attribute, Issue, Human Task, Marketplace Schema (L827-931) | gap | Provenance, issues, tasks missing; schema unversioned | G1, G5, G7 |
| MRP, price, tax, HSN, package dims per variant (L369-371, L1868-1946) | gap | CSV `mrp_inr/price_inr/weight_grams`, proposed as `products.mrp/price/weight_grams` (DB §5.2); no tax, HSN or package dims | G2 |
| Audit event with a reason (L823-825, L927-931) | gap | No `reason` column (CANON.md:94) | G11; erasure ADR (KNOWN-ISSUES.md:33-34) |
| Replay a failed run with its exact input (L12059-12061, L5192-5202) | gap | `input_digest` only; context rebuilt (CANON.md:118) | Q26 |
| Errors queryable by field/code/source/marketplace (L11187, L11489) | gap | JSONB, no endpoint | `GET /api/v1/errors` after S14 |
| Tenant-scoped image paths, versions, lifecycle (L10191-10193, L12991) | gap | `storage_key` convention unspecified | G12 |

Aligned: `tenant_id` enforced at the repository (L7335-7337, L10977-10991), plus RLS once
KNOWN-ISSUES.md:30 is fixed; `brand_id` superseded (CANON.md:47); versions stored in MVP (L9862-9868);
AgentRun a superset with `cost_usd` (L8732-8756); index plan (L11201-11205; CHECKLISTS.md:149);
HNSW (CANON.md:95) as in the plan (L5813, L5906); spec [183]'s IVFFlat (L12945) is superseded.

### 4.10 API and jobs

| Requirement (source) | Status | Our side | Action |
|---|---|---|---|
| ~10 unversioned endpoints (L6894-6962, L11239-11291) | superseded | `/api/v1` (CANON.md:33) | Tell client/FE; ARCHITECTURE.md:71 stale |
| Redis + ARQ on Memorystore (L4777-4781, L11869-11897, L12963-12977) | superseded | ADR-0004: procrastinate on Postgres, no Redis (spike-gated) | C6 |
| Queue per job type; per-marketplace concurrency (L11897) | superseded | Queues `default`/`publish` (ADR-0004) | G17 |
| Import enqueues generation per row (L7804-7806, L11345, L12075) | gap | Import ends at ImportReport | G10, Q11 |
| Cancel and history APIs for agent runs (L11475) | gap | `cancelled` state, no endpoint | `POST /api/v1/jobs/{id}/cancel`, queued only |
| One error format on every endpoint (L11295-11299) | gap | No `MAKER_CHECKER_VIOLATION`; path validator (KNOWN-ISSUES.md:26) | Fix in S2 |
| Retry a failed job "from the exact point of failure" (L5339); resume from the blocked step after a human answer (L2223) | gap | Not specified: a requeue inserts a new `job_runs` row (ADR-0004 §7); only publish retry is per-field (inv 10) | S13 plan; ties to G5 |

Aligned: 202 + job id + polling (L11359-11377; CANON.md:60); job states superseded by JobState +
`JOB_TIMEOUT` (L11911; CANON.md:51); backoff 1 s doubling, 60 s cap (L11973); dead letters with full
context (L12045-12047; ADR-0004 §7); timeouts (ADR-0004 §5; spike criterion 3); webhooks Phase 2
(L11503). The partial-field retry cap of 3 (L11925-11959) is written nowhere yet: put it in the
publish ADR.

### 4.11 Security and secrets

| Requirement (source) | Status | Our side | Action |
|---|---|---|---|
| Secrets in AWS Secrets Manager (L12540) / GCP Secret Manager as env-from-secret bindings (L5781, L5835-5837) | aligned with the plan | ADR-0001: Secret Manager, injected as env vars | — |
| Rotate secrets without a deploy (L12540) | conflict (minor) | Env-var secrets rotate by redeploy (ADR-0001) | Tell the client; cheap with zero-traffic deploys |
| TLS on API, DB and storage connections (L12554, L13647); "TLS 1.3 everywhere" (L6586) | gap (doc) | ADR-0001 uses Cloud Run's managed Cloud SQL connection; nothing states the encryption | State it in ADR-0001 / S4 |
| Private network to the DB (L13005, L5851-5855: private IP via VPC connector) | conflict | ADR-0001: built-in Cloud SQL connection, no VPC connector, no authorized networks | C7 |
| Audit every credential access (L12629) | gap | No audit action | G11 |
| Review AI provider retention (L12601) | client | Not planned (catalogus Q9) | Q22 |

Aligned: AES-256 tokens, keys outside code (L12526; inv 6; pin AES-256-GCM in the crypto ADR);
adapter-only per-tenant credentials (L12629) with per-secret IAM (KNOWN-ISSUES D); rate limits, CORS,
body cap (L12569-12573; ADR-0005); prompt-injection defences (L12615), with the `pattern` rule (G6).

### 4.12 Infrastructure, hosting and cost

| Requirement (source) | Status | Our side | Action |
|---|---|---|---|
| AWS (specs L12867-12869, L13005) -> GCP (plan L5801-5855) | aligned with the plan | ADR-0001: GCP asia-south1, two projects (STATE:98-101) | Tell the client why not AWS credits (C7) |
| Infra ₹6,700/month, ₹8-11k with LLM (L13095-13155); plan $150-250 (L7057-7061) | conflict (specs) | ADR-0001: ~$195-205/month prod + staging (~₹16.5-17.5k), before GST | C7, Q13 |
| Budgets per agent, marketplace and tenant (L12247) | gap | Global + per-tenant USD caps (ENV.md:97-98) | Q19 |
| EC2 + git pull (L13019); release branch + freeze (L5413-5450) | superseded | Tags, zero-traffic Cloud Run revision, auto-rollback (STATE:44-47, :60-61; ADR-0001) | ADR-0009 |
| One trace from request to marketplace (L12677, L5381-5385) | gap | Only `request_id` enters jobs | Carry W3C `traceparent` in job payloads (S11) |
| Staging uses test marketplace accounts (L13049, L13083) | client | `AMAZON_USE_SANDBOX`; no Flipkart switch | Q16 |

Aligned: no Kubernetes/Kafka/microservices/vector DB (L12875, L4807-4813); local, staging, prod
(L13031-13083; STATE:48-49); storage behind an interface (L5853; ADR-0001 `StorageAdapter`); backups
and restore under 15 min (L6782-6784; ADR-0001 PITR 7 days, still to be rehearsed).

### 4.13 Testing and launch

| Requirement (source) | Status | Our side | Action |
|---|---|---|---|
| Solo milestones by Day 7 (L4873-4968) | gap | Pre-S1 on Day 7 (STATE:14) | Re-baseline; tell the client before Day 11 (Thu 1 Oct) |
| UAT with realistic + messy products, launch gate 9 (L6732-6738, L6800-6802, L13083) | superseded | Automated only (STATE:50-52), yet STATE:111 lists "UAT users" | C8, Q15 |
| Failure injection: network, model timeout, DB (L13627) | gap | None | ADR-0010 |
| Rollback under 15 min, launch gate 10 (L5424, L6806-6808) | gap (proposed) | A previous tag can't undo a migration; DB §8.3 proposes expand/contract | Put it in ADR-0009 and rehearse |
| 17 BRD acceptance criteria (L2146-2164) | gap | At least 6 fail today: provenance, field classes, questions, status read, auto-fix, LIVE | Automated acceptance suite; §21 walkthrough (L2084-2144) as E2E fixture |
| 10 BRD metrics (L2166-2210) | gap | Raw events missing: live confirmation, questions, recurrence, attempts | Capture via G3-G5; views later |
| Prod OAuth verified; Day-1 filings (L4883-4885, L13668) | client | Filing status unknown (STATE:111) | Q12 |
| Incident response plan (L13671) | gap | No ops owner | Q18 |

Aligned: the test matrix (L6664-6714; our 4 gates are stricter, and ADR-0010 should name each client
suite); Playwright + Locust at 1k rows / 20 concurrent (L6794-6796; STATE:40), still needing a
Playwright owner; the Day-41 security audit (L5387-5398).

### 4.14 Roadmap and non-goals

All aligned:
- **Non-goals:** BRD L187-217 and plan L4807-4813 match AGENTS §4. Add the BRD's inventory, pricing
  and returns items there.
- **Phase 2:** version compare, prompt A/B, webhooks (L13293-13299). `listing_versions` and
  `prompt_version` exist now, so ask the AI team to fill `prompt_version` from day 1.
- **Phase 3:** auto-publish, a scheduled Optimization Agent, the learning loop (L13315-13387). The
  loop's training signal (reject reasons) must be captured from v1 (G4).
- **Invariants:** the client's 14 (L13690-13703) map onto our 10: 1->1, 2->2, 3->3, 4+5->4, 6->5,
  7->6, 8->7, 9->8, 10->9, 11+12->10. #13 (scheduled Optimization Agent) is Phase 3; #14 (no core
  rewrite) is a design principle. Show the client this map so it doesn't read as "four dropped".
- **Later:** more marketplaces (catalogus move 7 `publish_mode`); PIM/ERP/URL intake; multilingual
  content; SP-API notifications (delivered via AWS SQS/EventBridge).

## 5. Conflicts

| # | The vision says | We decided / the Canon says, and why | Recommended resolution | Decides |
|---|---|---|---|---|
| C1 Autonomy | Autonomous submission, approval optional (L2243, L39-41, L1129-1133); "autonomous by default, but not reckless" (L1362); the final chain has no approval step (L2283) | Invariants 1 and 3; STATE:63-65 keeps approval in v1 with `auto_publish` OFF. The client's own plan and specs agree with us (L4700, L4765-4769, L4812, L7263-7274, L7405, L7483, L11786-11788, L13692; auto-publish Phase 3 after N consecutive unedited approvals, L13315, L13359) | Keep the v1 ruling. The founder confirms the long-term path and unlock granularity. Make approval fast with batch approve (G10). ADR-0008 | Client (Q1), user |
| C2 Published vs LIVE | Success = the marketplace confirms it live and its ID is shown (L183-185, L1079-1087) | `published` comes from the adapter result (CONTRACTS.md:623-650), exactly as the plan and specs describe (L6129, L8161, L8612); only the BRD asks for confirmation. An SP-API `ACCEPTED` is not live (verify in the adapter step) | Keep 9 statuses; add marketplace-side state, identifiers and a bounded sync job (G3); UI shows "Submitted" until confirmed | User (ADR), FE lead, client (Q2) |
| C3 Status vocabulary | 14 statuses (L1101-1193) | 9, "any extra value" forbidden (CANON.md:53). STATE:112 and KNOWN-ISSUES.md:12 say "16". The Canon's rejected "14 uppercase stages" are the plan's (L6107-6134), so nothing ruled on the BRD | Keep 9 persisted values. Add `marketplace_state` (processing/live/partial), `paused_at`, `archived_at`, a derived `human_action_required`, `review_decisions` for spec [17]'s REJECTED (L8609), and a BRD-named display projection for the FE | Client (Q3), user, FE lead |
| C4 Intake template | One column per field (L1560-2082) | A JSON `attributes` cell (<= 50 pairs). Our `title` is the seller's name; the BRD's `title` is the *generated* one (its seller name is `product_name`) | ADR before S13: named columns for the BRD core + compliance/commercial/identifier fields, JSON for the long tail; rename or document `title` | User, FE lead |
| C5 CanonicalListing shape | [06] has flat title/bullets/description + `images[]` (L6051-6094); [35] nests them under `content` and adds `category`, `status`, `image_reports`, `version` (L9643-9673). Both carry `keywords.backend_keywords` | Nested + `hidden_terms` + sibling `images[]` (DECISIONS D3); keeps inv 4 (Flipkart has no backend keywords, L9840-9844), but no Canon row | Canon row + ADR before `schemas-v1.0.0`; tell the client | User, AI lead, FE lead |
| C6 Stack deltas | Python 3.11 (L5737), PG 16 (L5753), Redis + ARQ, which the scope-lock left column names (L4779, L12963-12977), NextAuth JWT (L11517). (IVFFlat, L12945, is spec-only; the plan already uses HNSW) | Python 3.13 (ADR-0002; AI team to confirm), PG 17 (ADR-0003), procrastinate with no Redis (ADR-0004, spike-gated), HNSW, FastAPI issuer; reasons in docs/research/stack-2026-09.md | Send the client a one-page note; the AI lead confirms Python 3.13 and LangGraph (STATE:89) | User tells the client; AI lead |
| C7 Cloud, network, cost | Specs: AWS at ₹6,700/month infra (L13155). Plan: GCP at $150-250 (L7057-7061), DB over private IP via a VPC connector (L5851-5855) | ADR-0001: GCP asia-south1, ~$195-205/month prod + staging before GST, Cloud Run's built-in Cloud SQL connection with no connector. AWS + Activate ranked first only if the client is incorporated (unknown) | Tell the client: their cloud, inside the plan's range but not the specs' ₹6,700; explain the network choice. The client sets a year-2 ceiling (Q13); ADR-0001 revisits above ₹25k/month | Client (ceiling, incorporation) |
| C8 UAT | Whole-team UAT, launch gate 9 (L6732-6738, L6800-6802), on staging (L13083) | Automated only (STATE:50-52) | Keep automated gates with the §21 walkthrough + a messy product as fixtures; offer optional design-partner acceptance | Client (Q15) |
| C9 Analytics, performance | Plan: operational analytics (volume, approval, validation pass, publish success) is Phase 1, in the DoD and under launch gate 1 (L4690, L4771-4773, L5368-5373, L6754, L13279); "automated performance tracking" is Phase 2 (L6880, L13297). Specs: performance collected and shown in MVP (L8189, L11131), `GET /analytics` = performance + sales attribution (L11285, L11447) | "Phase 2" (ARCHITECTURE.md:69, which lists spec [153]'s Phase-2 AI metrics); metric views killed (catalogus); the Amazon app asks only for Listing/Inventory/Catalog roles (BLOCKERS.md:32), which likely lack impressions, clicks, rank (verify) | Ship the plan's four operational metrics in v1 (cheap: `job_runs`, `review_decisions`, `validation_runs`, `publish_field_results`); treat marketplace performance as Phase 2 per the plan unless the client says otherwise (Q10); settle SP-API roles *before* filing, since a change re-enters the 2-4 week review | Client |
| C10 One-person brands | "Even if a single person operates the platform... explicit review" (L7483) vs the Day-45 DoD's user who "can independently ... generate, approve, and publish" (L5450); yet the Day-45 DoD bars the creator from approving (L4689), as does Day 34 (L5313) | Generator or editor never approves (inv 3; 0004) | Second member required at invite; staff-review tier later (`actor_type=staff`); acceptance tests use two users | Client (Q5) |

## 6. Gaps worth closing

### 6.1 Foundation (backend + DB), ranked

Ranked by leverage and by the cost of a MAJOR change later. Effort is rough, in engineer-days incl.
the ADR. About 4 days overlap catalogus moves 1-6.

| # | Gap | Source | Lands in | Days |
|---|---|---|---|---|
| G1 | **Attribute provenance:** envelope `{value, normalized, source: seller/document/pim/marketplace/derived/ai, confidence, evidence_ref, updated_at}`; a per-category critical-field list as data; a rule "critical field source != ai". Touches the `AttributeValue` contract. | L765, L867-871, L933-941 | 0001, schemas-v1.0.0 | 1-1.5 |
| G2 | **Compliance, commercial and identifier fields:** brand defaults (legal name, owner type, GSTIN, address, consumer care, manufacturer/marketer/packer/importer, origin) on `tenants` or `brand_profiles`; per product/variant `mrp`/`price` NUMERIC(12,2) INR with `CHECK (price <= mrp)`, HSN, tax rate, origin, identifier type + value or exemption, package L/B/H/weight, size system, size declaration with metric measurements, generic name, net quantity. Flipkart publish needs the commercial set (L1526-1530), so this also needs a product-facts input to the adapter. | L245-333, L1430-1508, L1520-1530, L1868-1946 | 0001 | 1 |
| G3 | **Publish ledger + LIVE confirmation:** catalogus move 1 (keep `external_id`; `published_version_id`, `first/last_published_at`, `publish_field_results` keyed by child SKU); `listing_submissions` (payload hash + pointer, reference, attempt, outcome); `marketplace_state`, `marketplace_sku`, `live_url`, `live_confirmed_at`, `last_synced_at`; a bounded `listing_status_sync` JobKind (MINOR). Polling first; webhook/notification listeners (L149) later. | L139-173, L1039-1087, L2222, L8161-8167 | 0002/0003; job with adapters | 1.5 + 2-3 |
| G4 | **Review decisions** (catalogus move 2) + optional `GenerationContext.review_feedback` (MINOR) + `listing_versions.origin`. | L8107-8127, L13387 | 0002 | 0.5 |
| G5 | **`listing_issues` + `human_tasks`:** issues are fed from ErrorItems, status open/auto_fixed/resolved/dismissed; tasks carry a field path, question, expected answer, suggestions and answerer. An answer updates the fact and re-runs only the affected fields (inv 10). | L909-925, L1027-1037, L1360-1386, L2220-2223 | 0002; seam later | 0.5 + 1.5 |
| G6 | **RuleDef types:** `allowed_values`, `conditional_required`, `cross_field`, `pattern` (catalogus move 4), `min_items`, image thresholds per slot, `claim_requires_attribute`, `policy_ref`; fix the unique key and version rule (KNOWN-ISSUES.md:23-24). | L999-1007, L10135-10179, L10426, L11561 | schemas-v1.0.0 | 1.5 |
| G7 | **Versioned `category_templates`:** `version`, `source` (ptd/manual), `fetched_at`, `is_current`, `product_type`, per-attribute `{marketplace_field, value_map}`, canonical keys; store the template version beside `rules_version`. | L224-228, L855-859, L10633 | 0003 | 0.5 |
| G8 | **Variant model:** a colour level or colour-keyed images; unavailable combos; one listing per child or per family; a family ListingResult; neutral `variation_theme`. | L599-640, L903-907 | ADR-0007, before 0002 | 0.5-1 |
| G9 | **Validation context + approve precondition:** `validate()` gets product facts; approve and publish assert a passed `validation_run` for that exact version at the current `rules_version`. | L1388-1428, L7323-7325 | S2; S10/S14 Done-when | 0.5 |
| G10 | **Batch generate/approve/publish** + `job_runs.batch_id` (catalogus move 3); the import -> generate policy; the per-row approval UI after import. | L7427, L7804, L7922, L11345, L12075 | schemas-v1.0.0 | 0.5 |
| G11 | **Adapter ABC** frozen with `get_performance` and a network-free `preview()`. **Audit:** `audit_log.reason`, a credential-use action, nullable tenant for platform changes. | L6497-6536, L8089, L823, L12629 | ~Day 29; 0002 | 0.75 |
| G12 | **Images:** per-(image, marketplace) verdicts, `content_sha256`, `original_filename`/`seller_ref`, detail/back/packaging_label roles (label never published), tenant-prefixed keys, an audited "accept warning". | L641-691, L10019-10029, L10191 | 0002 | 0.5 |
| G13 | **`brand_chunks.type` CHECK**, approve -> `brand_embed`, nullable `weight`. | L5895-5906, L10398 | 0002 | 0.25 |
| G14 | **Performance ingestion:** adapter fetch, scheduled job, idempotent upsert, only if C9 keeps it in v1 (the plan says Phase 2, L6880). | L8179-8189, L11131 | Adapters | 1.5 |
| G15 | **`locale`** (default `en-IN`) on CanonicalListing and versions (MINOR). | L2245-2247 | schemas-v1.0.0 | 0.1 |
| G16 | **`listings.status` transition table,** incl. blocked -> needs_review after edit + revalidate. | L8603-8684 | S2 | 0.5 |
| G17 | **Outbound limiter:** a Postgres token bucket per (marketplace, account, operation). | L10559, L11985-12001 | Adapters | 1 |
| G18 | **Error taxonomy as data:** a (marketplace, raw code) -> code/class/auto_fixable/plain-text table; a 4-class retry map; new codes (MAKER_CHECKER_VIOLATION, identifier, permission, variation, compliance, unknown). | L1270-1354, L11744-11774 | 0003; codes.py | 0.75 |
| G19 | **`marketplace_accounts.settings`** (fulfilment/SLA, locations, brand-registry and category approvals, GTIN exemption) + a `needs_reconnect` status. The client escalates after one failed refresh (L11943-11947); DB §5.4 proposes three. | L293-297, L335-371, L7967-7969 | 0002 | 0.25 |

**Tier A**, which must be in 0001-0003 / `schemas-v1.0.0`: G1-G8, G10, G15. The rest is additive and
ships with its build step.

**Already proposed in DATABASE.md** (so only the remainder is new): G2 `mrp`/`price` with
`price <= mrp`, `gtin`, `weight_grams` (not HSN, origin, identifier type, package dims or the brand
profile); G3 `external_id`, `published_version_id`, `first/last_published_at`,
`publish_field_results` (not marketplace state, live URL, sync or submissions); G4 `review_decisions`
and `listing_versions.origin` (not `GenerationContext.review_feedback`);
G7 `version`/`enabled` (not source, fetch date, product type or value maps); G8 one-level families
with a canonical `variation_theme` (not the colour level or unavailable combos); G10 `batch_id`; G12
`content_hash` and dimensions; G13 the type vocabulary (not `weight`); G16 `listings_status_guard`;
G19 `status` with `reauth_required` and `refresh_failures` (not `settings`). G1 and G5 are absent.
(DATABASE.md is still being edited; re-check this list against its §5 before the 0001 ADR.)

### 6.2 Other teams and later phases

- **AI team:** understand/classify/conflict nodes (L943-1025); structured questions in
  GenerationResult (for G5); claim grounding; the confidence-formula ADR (by Day 21); an
  escalation-model port; a relevance threshold (L10325); fill `prompt_version`; the stale-errors bug
  in `state.py` (KNOWN-ISSUES C); confirm Python 3.13 and LangGraph.
- **FE team:** the BRD dashboard (list + 10 panels, L1195-1268) on the C3 projection; the approval
  screen with the adapter preview; error paths from `paths.py`, not the spec examples (L8043-8077);
  the ListingResult vocabulary; NextAuth as a wrapper only.
- **Later:** webhooks, version compare, unified analytics; auto-publish, the Optimization Agent and
  the learning loop; Myntra/AJIO/Meesho/Nykaa (file export); PIM/ERP/URL intake; connect-and-audit
  of live listings (the BRD reuses them as a data source, L380, L1020); multilingual content; cloning
  approved listings (L12267).
- **Searched and absent from the source:** Shopify, ONDC, A+/enhanced brand content, product pricing
  plans or billing. Nothing to align; do not add them without a client ask.

## 7. Questions

Ordered by what they block first. **[C]** = client, **[U]** = user. The default is what we build
without an answer.

1. **[C] Long-term autonomy and unlock granularity?** *Default:* approval mandatory in v1; Phase-3
   unlock per tenant x marketplace after a track record; the v1 column is a placeholder. (Before 0001.)
2. **[C] What counts as "listed"?** *Default:* the BRD's definition. LIVE only after marketplace
   confirmation, polled in v1; "Submitted" until then.
3. **[C] Dashboard status names?** *Default:* 9 persisted statuses + orthogonal columns, shown through
   a BRD-named projection.
4. **[C] May a deterministic fix after a marketplace rejection resubmit without re-approval?**
   *Default:* only for adapter-side remaps with canonical content unchanged, audited as `system`. Any
   content or fact change creates a new version that needs approval.
5. **[C] One-person brands?** *Default:* a second invited reviewer/owner is required; a staff-review
   tier later.
6. **[C] Several brands behind one Amazon/Flipkart seller account?** *Default:* no; tenant == brand.
   If yes, a `brands` table goes under the tenant before 0001.
7. **[C] The two launch categories,** with Amazon product type/browse node and Flipkart vertical?
   *Default:* two apparel categories; the seed's non-apparel tenant (Addendum §5) becomes apparel.
8. **[C] Which Legal Metrology declarations block approval, and who signs the rule text?** *Default:*
   block on manufacturer/marketer/importer, origin, MRP incl. taxes, consumer care and size
   (the 2022 garment amendment's list, L1508); warn on generic name, net quantity and the rest; the
   client's adviser signs.
9. **[C] Which attributes may AI infer?** *Default:* never fibre composition, origin, care, MRP, HSN,
   identifiers, certifications or measurements. Others may be inferred, with the source shown.
10. **[C] Launch analytics, and are marketplace performance numbers v1?** The plan says operational
    metrics only, performance Phase 2 (L4773, L6880); the specs say performance in MVP (L8189,
    L11131). *Default:* the plan: volume, first-pass validation, approval rate, publish success,
    cost per listing; no extra SP-API roles.
11. **[C] Does an import auto-generate?** *Default:* no; the user picks rows and marketplaces through a
    batch contract.
12. **[C] Status of the Amazon/Flipkart filings and the apex domain; were the wrong kickoff-pack URIs
    filed?** (STATE open decisions 5, 7) *Default:* file now with the Canon URIs.
13. **[C] Year-2 infra ceiling and incorporation/GST/DPIIT status (it gates the credits)?** *Default:*
    ADR-0001's ~$195-205/month for prod + staging, excluding LLM; revisit above ₹25k/month.
14. **[C] Approve the scope additions** (variants, memberships, tenant lifecycle, `auto_publish`) under
    the left-column rule (L4733)? *Default:* treat as approved; flag them in the next update.
15. **[C] Automated E2E in place of launch gate 9 (UAT)?** *Default:* yes, plus optional
    design-partner acceptance.
16. **[C] What may staging publish to?** *Default:* Amazon sandbox in CI, a test SKU on the client's
    accounts for staging, FakeAdapter for Flipkart until a sandbox exists.
17. **[C] Confirm the Canon D13 role defaults?** *Default:* accept them.
18. **[C] Who does ops** (blocked items, DLQ, SKU conflicts, incidents)? *Default:* the tenant owner
    for their blocked items; our staff for DLQ and system failures via `/internal/admin`.
19. **[C] At the LLM budget cap, stop or degrade?** *Default:* a hard daily stop with
    `BUDGET_EXCEEDED`; the cap is set by `tenants.plan`.
20. **[C] Catalog sync / live-listing import in v1?** *Default:* no; Phase 2.
21. **[C] Social login in v1?** *Default:* no; Google later, matched to the invite email.
22. **[C] AI-provider retention; India-region endpoints?** *Default:* no-training API terms; no
    India-region need (STATE:39).
23. **[C] Swatch images; a third marketplace in v1?** *Default:* no and no.
24. **[U] Accept G1-G8, G10 and G15** (with catalogus moves 1-6) into the 0001-0003 designs?
    *Default:* yes, with ADRs as `proposed`.
25. **[U] Re-baseline the schedule** and tell the client before Day 11? *Default:* yes, now that ADR-0001
    is accepted.
26. **[U] How does a failed run replay** (L12059-12061)? *Default:* same request, fresh context; tell
    the client.

## 8. Recommended context updates (not applied)

**AGENTS.md**
- **§1:** add "Client vision: the Founder BRD (docs/source L10-2286) is the product requirement; the
  build plan and specs are the architecture; our deviations are in VISION-ALIGNMENT.md §5 and must be
  told to the client."
- **§3:** point to the 14->10 map (§4.14). Note on invariant 3: the BRD's autonomy is Phase 3 at the
  earliest (ADR-0008).
- **§4 non-goals:** add inventory, pricing optimization, orders/returns and image generation
  (L187-217, L2249-2251).
- **§5 glossary:** BRD "canonical product record" = `products` + variants; `CanonicalListing` =
  per-marketplace content. Once C2 is ruled, add "`published` != live".

**docs/STATE.md**
- **Open decision 6 (line 112):** "16-state" -> "14 statuses (BRD §13, L1101-1193)". The v1
  approval part is resolved; what remains is Q1-Q3.
- **Next action 2:** mark this file done.
- **Add client items:** Q4, Q5, Q6, Q8, Q9, Q10, Q13. Add "SP-API roles vs performance data" and
  "Flipkart sandbox" to open decision 5.
- **Testing decision:** note that it overrides client launch gate 9 (C8), and reconcile it with the
  "UAT users" item.

**docs/review/KNOWN-ISSUES.md**
- **A:12:** the BRD has 14 statuses; the split pages duplicate the Complete BRD; v1 approval is
  settled by STATE; see C1-C3.
- **A:15:** add PARTIALLY_LIVE/PAUSED/ARCHIVED/HUMAN_ACTION_REQUIRED (G3-G5). **A:17:** operational
  analytics is Phase 1 for the client (DoD, launch gate 1); performance is Phase 2 in the plan but MVP
  in the specs (C9).
- **New A rows:** published != live (C2); one-person brand vs the DoD (C10); SP-API roles vs
  performance data; the Flipkart sandbox; multi-brand seller accounts (Q6).
- **New B rows:** approve without a passed validation of that version (G9); per-marketplace image
  verdicts (G12); image thresholds not data (G6); no `brand_chunks.type` vocabulary (G13); no
  `traceparent` in jobs; DB connection encryption not stated (ADR-0001); `external_id` is in both
  client DDLs (L5938, L10944) but in no Canon row, which is why catalogus move 1 and DB §5.3 re-propose
  it; no contract passes commercial facts to the adapters (Flipkart needs them, L1526-1530).
- **B:33-34:** add `reason` and credential-use events (G11).

**docs/architecture/DATABASE.md** (add the remainder in §6.1's note, each tagged Proposed with its
pending decision)
- **§11.1 row 7:** "Founder BRD's 16 states" -> 14. Rows 15-16 (per-family vs per-child, performance
  roles) match G8 and C9; add the BRD refs.
- **0001:** the brand compliance profile and the product HSN, origin, identifier type/exemption,
  package dims and size system (G2); the provenance envelope on `attributes` (G1); matrix
  availability or a colour level (G8).
- **0002:** on `listings`, `marketplace_sku`, `marketplace_state`, `live_url`, `live_confirmed_at`,
  `last_synced_at`, `paused_at`, `archived_at` (G3, C3); `listing_versions.locale` (G15); new
  `listing_submissions`, `listing_issues`, `human_tasks` (G3, G5); per-(image, marketplace) verdicts
  (G12); `brand_chunks.weight` (G13); `audit_log.reason` + credential-use actions (G11);
  `marketplace_accounts.settings` (G19).
- **0003:** `category_templates` `source`, `fetched_at`, `product_type`, value maps (G7); the
  error-mapping table (G18).

**Other stale lines:**
- ARCHITECTURE.md:69 ("Phase 2" analytics) and :71 ("no /v1/ prefix").
- BUILD-SEQUENCE.md:65 (three GCP projects; ADR-0001 has two).
- STATE next action 2: DATABASE.md already follows ADR-0004 (no Redis; `oauth_states`,
  `rate_limit_counters`, DB §5.4), so drop the "must be reconciled" note.

## 9. Verification log (skeptical review, 2026-09-27)

Every C/G item and every conflict/gap/client row was re-read against the cited source lines and
against AGENTS.md, STATE.md, CANON.md, the Addendum, ADR-0001/0004, DATABASE.md, CONTRACTS.md,
catalogus.md and the scaffold. Keyword sweep done (autonomous, approve, variant, size, colour,
Legal Metrology, MRP, HSN, GST, status, onboarding, pricing, plan, tenant, brand, Shopify,
Myntra/Meesho/Ajio/Nykaa, ONDC, image, A+, analytics, KPI, SLA). The split BRD pages were
re-diffed against the Complete BRD: identical apart from page headers.

**Corrected (the claim was wrong):**
- Summary 3 / C3: REJECTED is spec [17]'s (L8609), not a BRD status; PROCESSING is the BRD's
  missing one. Site page [06] itself says 14 states (L3417).
- §3: the spec DDL has 5 statuses (L10943), not 9; L11061 is prose.
- §4.1 / C9: launch gate 7 is Observability (L6788-6790). Analytics sits under gate 1 (L6754) and
  the DoD (L4690). The plan puts performance tracking in Phase 2 (L6880, L13297); C9 was rewritten.
- C5: both CanonicalListing drafts carry `backend_keywords`; the real difference is flat vs nested
  content and the extra [35] fields.
- C10 / §4.2: the "independently ... approve, and publish" quote is L5450, not L4681; the same DoD
  bars self-approval (L4689).
- §4.2: the plan also bars editors from approving (L6620); the old text implied only we do.
- §4.3: parent/child separation is L638 (L637 is "keep seller SKU stable").
- §4.8: CHECKLISTS.md:254 (not :253) *assumes* marketplace dedupe; "one refresh" is L11943-11947.
- §4.10: the timeout is spike criterion 3, not 8.
- C6 / Summary / §4.9: HNSW is the plan's own choice; IVFFlat is spec-only.
- §4.9: CANON.md:93 never mentions `attributes` (now cites DB §5.2 and CONTRACTS.md:245); `mrp`,
  `price`, `weight_grams` are no longer CSV-only (DB §5.2 proposes columns).
- §8: the "catalogus move 1 is wrong" note was overstated (no Canon row names `external_id`);
  the DATABASE.md-vs-ADR-0004 item is already done.

**Added (missed requirements):** extra Legal Metrology declarations (L1446-1508); Flipkart's
commercial-field dependency and the missing adapter input (L1520-1530); brand-registry/category
permissions (L293-297, L963, L1326-1330); webhook feed listeners (L149); retry from the point of
failure / resume (L2223, L5339); per-row bulk approval (L7427, L7922); four new client
self-contradictions (analytics, maker-checker scope, vector index, variants only in the BRD);
the plan's chunking owner (L5083); an "absent from source" note (Shopify, ONDC, A+, pricing plans).

**Confirmed, citations only added:** C1 (L1362, L4700, L4812, L7405, L13359), C2 (the plan and
specs set `published` from the adapter result too: L6129, L8161, L8612).

**Checked, no change:** C4, C7, C8; G1, G6-G9, G11-G13, G15-G18; the 14->10 invariant map; the
64-column, 17-criteria, 10-metric, 11-error-class, 9-gate and 16-record counts; the cited STATE,
KNOWN-ISSUES, ADR-0001 and Canon lines other than those corrected above.
