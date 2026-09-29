# Competitor research: Catalogus (and Catalogix)

_2026-09-26. Read-only public research by a 5-agent workflow: 3 sweeps, a gap analysis, and a
skeptic who re-checked the sources. Nothing about our project was entered into any external site._
**Status: research note, not authority.** The "Foundation moves" section is a set of proposals; each
one needs the user's OK and an ADR before it touches the Canon.

## Catalogus: who they are

- **Company:**
  - Sombrero Digital Commerce Pvt. Ltd., Gurugram, founded 2023.
  - Founders came from ANS Commerce, AdYogi, P&G and JioMart.
  - Undisclosed pre-seed in Apr 2025, led by Zeropearl VC; the Mokobara founders are angels.
  - About 17–19 staff.
- **Positioning over time:**
  - 2024: ONDC "cataloguing as a service".
  - 2025: an "AI PIM" for Amazon, Flipkart, Myntra, Noon and quick commerce.
  - 2026: a global "All-in-One AI Platform for Product Content".
- **The 2026 product** has five modules: an AI PIM, AI enrichment, an AI Image Studio (on-model
  photos and backgrounds), a DAM, and syndication to "50+" connectors.
  - Named connectors: Amazon, Shopify, Google Shopping, Walmart, eBay, Zalando.
  - **Flipkart is gone from all 2026 copy and app code.** It was listed in Dec 2025.
- **Pitch:** autonomy. They say "publish to every channel, autonomously", and "autonomously"
  appears 15 times. Human review is "only needed for edge cases". They also claim approval chains,
  audit trails and "built-in validation rules" next to "AI-led checks".
- **Sales:** enterprise and founder-led. No public price, no trial, "free pilot for qualified
  teams", and a USD anchor of "$62K–$210K/yr vs $310K–$960K for 6 tools". US intent: eTail Palm
  Springs, Enterprise SSO listed.
- **Proof:**
  - Named users: Snitch ("800 styles on 10 marketplaces" with about 1 hour of QC for one person),
    Mokobara and Vishal.
  - The other logos (Myntra, Nykaa, Tata CLiQ, Snapdeal) are marketplaces, so more likely channels
    than customers.
  - No G2 / Capterra / Trustpilot reviews found.
- **Tech (as observed):**
  - Marketing site: Vite/React on Vercel.
  - App (`app.catalogus.in`): Next.js with NextAuth, plus an email one-time code at login.
  - Backend: Node/Express on AWS ap-south-1, with S3.
  - Tracking: PostHog (US), Clarity, Sentry.
  - The app's route map shows import/export mappers and marketplace templates. That suggests
    **file/template syndication rather than per-marketplace API adapters** (inferred).
- **Trust gaps:** no SOC2/ISO/DPDP claims, no ToS/DPA/subprocessor list, no status page, no public
  API, and no statement on which AI providers they use or whether they train on customer data.

## Second competitor found: Catalogix (Streamoid). Not yet researched in depth.

- It claims "Nothing goes live without the right sign off", with role permissions, a full audit
  trail, workspace isolation and in-region data.
- It publishes to **Flipkart, Amazon, Myntra and Meesho** with "variation groups", and has a
  Capterra listing.
- **Consequence:** in India, "safety chain" and "Flipkart depth" are only *parity* claims unless we
  make them **provable**. A dedicated Catalogix sweep is needed before any public positioning.

## Where they are ahead

| Area | Matters for v1? |
|---|---|
| Channel breadth (50+), AI image generation, DAM, supplier onboarding, agency white-label, multilingual | No. Roadmap or partner candidates; don't chase them in 45 days. |
| PIM depth: variants, attribute sets, health score | Yes, **variants** if the launch categories are apparel |
| Ingestion breadth (Excel, XML, Shopify import, mappers) | Partly. CSV now, the rest later |
| Post-publish ops: rejected-listing alerts, change propagation | Yes, the **data** for it (see moves 1 and 7) |
| Distribution and proof in Indian fashion D2C | Yes. This is the real threat |

## Where we can credibly win

1. **Governance a seller can verify, not just be told about:**
   - Maker-checker enforced in the database.
   - Every error cites the marketplace or legal rule it enforces.
   - An append-only audit log.
   - A per-field record of what each marketplace actually accepted.
2. **India depth through official APIs:** Amazon SP-API plus Flipkart Seller API, with India rules
   (including Legal Metrology) kept as versioned data. Catalogus looks template-driven, and its
   Flipkart support has gone quiet.
3. **Self-serve, low-price INR entry for small and mid-size D2C brands.** Both competitors are
   demo-led.
4. **Proof measured on the customer's own data:** cost per listing, first-pass validation rate,
   approve-without-edit rate, and per-field acceptance. Their competitors offer testimonials.
5. **Honest trust material:** say "India-hosted" and name the subprocessors. Never claim "no data
   leaves India": OpenAI and Sentry process data outside it.

## Foundation moves (proposals; about 4–5 engineer-days, all inside the unwritten 0001–0003 and before the `schemas-v1.0.0` tag)

| # | Move | Why | Effort |
|---|---|---|---|
| 1 | **Per-field publish ledger.** `listings.external_id`, `published_version_id`, `first/last_published_at`, plus a table `publish_field_results`. | Invariant 10 needs it; fixes the missing "partially published" state and the fact that `ListingResult.external_id` has no column. | 0.5–1 d now vs several days after Day 29 |
| 2 | **Review decisions as data.** A table `review_decisions` (approve / reject / regenerate_requested, reason, feedback, field_paths) plus `listing_versions.origin`. | Fixes "rejected has no state" without adding a 10th status, and makes "who approved what, and why" a query. | 0.5 d |
| 3 | **Batch-shaped approve and publish contracts** (`items[1..200]`, per-item result) plus `job_runs.batch_id`. Bulk approve is allowed only for versions with zero errors and zero warnings, each checked by maker-checker and audited. | Mandatory approval only sells if it is fast. Changing single to batch later is a MAJOR three-team break. | free now, plus 0.5 d |
| 4 | **Rules as cited data.** `policy_ref` and `pack` fields in `rule_def`; fix the rules uniqueness key; add a platform-only, ReDoS-safe `pattern` rule type. | Makes "deterministic" visible and citable, and builds a corpus that compounds. | 0.5 d |
| 5 | **Apparel variation families,** only if the client confirms apparel: `products.parent_product_id`, `variant_attributes`, `variation_theme`; an optional `parent_sku` CSV column. | Table stakes for apparel; publishing variants as standalone listings gets rejected or fragments reviews. | 0.5 d now, 2–4 d for adapters later |
| 6 | **Tenant lifecycle and an erasure path.** `tenants.status/plan/trial_ends_at` plus a per-tenant LLM budget override, and an ADR that resolves `audit_log ON DELETE CASCADE` vs the append-only trigger. | Enables a self-serve trial, and is the only way to delete a tenant under DPDP. | 0.5 d plus an ADR before 0004 |
| 7 | **Adapter capabilities:** `publish_mode` (api / file_export) plus transport facts only. Content limits stay in `rules`. | A contingency if Flipkart API approval slips; Myntra, Ajio and Meesho likely need it too. | free now; exporter 2–3 d, only if needed |
| 8 | **Identity:** enforce globally unique `lower(email)` in 0001 now. Add `tenant_memberships` only if the client confirms agencies or multi-brand users. | Fixes the login-ambiguity defect, and lets one reviewer serve several one-person brands. | free now; memberships 1–1.5 d |

**Killed by the skeptic:**
- An fx_rates table: the Canon uses USD on purpose; convert at display time.
- An attribute_value_maps table: redundant with `category_templates`.
- A public API reference: `/docs` is 404 in prod on purpose.
- A free public compliance checker: it would hand our rules corpus to scrapers.
- Reserving new JobKinds now.
- Metric SQL views: analytics ratios are Phase 2.

## Questions for the client

1. **Launch position:** is mandatory human approval (maker-checker) confirmed, against the Founder
   BRD's "autonomous submission"? It is our clearest difference from Catalogus.
2. **The two launch categories:** apparel? If yes, must v1 publish size/colour variation families?
3. **Pricing motion:** self-serve INR (trial, per-listing or credit packs), or sales-led pilots? At
   what target price?
4. **One-person brands:** require an invited reviewer, or offer a paid "we review for you" tier? That
   needs staff tenant membership and an audited `actor_type=staff`.
5. **Agencies and multi-brand groups within 6 months?** This decides whether memberships go in now.
6. **Onboarding:** mostly new launches, or improving live listings? The second means connect-and-audit.
7. **Next marketplace after Amazon and Flipkart:** API or template upload?
8. **Which India compliance declarations must block approval** (Legal Metrology), and who signs off
   the rule text?
9. **Trust page:** publish a subprocessor list, a no-training statement, DPDP deletion terms and a
   status page? Does any customer require India-region model endpoints?
10. **"Basic analytics":** adopt the measured-proof metrics in "Where we can win" #4?
11. **Design partners** for UAT whose results we may publish, and any history with Catalogus's
    customers.

**Positioning draft:** _"Amazon.in and Flipkart listings in your brand's voice, with receipts: every
field checked against a cited marketplace rule, signed off by a second person on your team, and
tracked to what the marketplace actually accepted. Start self-serve, with no sales call."_
