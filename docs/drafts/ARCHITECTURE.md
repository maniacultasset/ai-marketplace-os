# Architecture overview

## In one line

It writes your product listings for Amazon and Flipkart, checks them against each site's rules, and only puts them live after a person approves.


## The problem

Direct-to-consumer brands want their products on Amazon and Flipkart, but each marketplace has its own writing rules, attribute schemas and image specs, and getting them wrong means rejection or a suspended account. AI Marketplace OS lets a brand connect its seller accounts once and upload its catalogue once; AI then writes the title, bullets, description, keywords and attributes for each marketplace in that brand's own tone of voice. The brands pay for it, and the reason it is hard is the safety chain rather than the writing: a language model will happily breach a 200-character title cap or slip in a banned word, so every draft must pass hand-written deterministic rules and a second human before any adapter is allowed to publish. The build is a 45-day sprint that started on 21 September 2026 and launches 4 November 2026 - and it is currently on Day 2 with four of its five engineers not yet on board.


## Who uses it

| Role | What they do |
|---|---|
| Owner (at the customer brand) | The brand's account holder. Full access - can import, generate, edit, approve and manage the team. Still cannot approve a listing they personally generated. |
| Operator (at the customer brand) | The day-to-day worker. Uploads the CSV or enters products by hand, triggers AI generation, edits copy. Explicitly cannot approve their own work - that is the maker-checker rule. |
| Reviewer (at the customer brand) | The second pair of eyes. Sees generated content with its validation results, warnings, errors, confidence score and the per-marketplace differences, then Approves, Rejects with feedback, Edits inline or Regenerates with instructions. Cannot import or generate. Every action records who and when against a version. |
| Admin (at the customer brand) | Handles settings, marketplace account connections and team roles. Deliberately has no data-modification rights. |
| Our operations person (undefined - and that is a gap) | Not a defined role in the docs, but a real job you must staff: clearing listings that hit BLOCKED after three failed AI corrections, chasing PARTIALLY_PUBLISHED and PUBLISH_FAILED listings, and resubmitting dead-lettered jobs. No owner, queue, notification path or response time is specified anywhere. |
| The AI agents | Four MVP agents: Content writes the title, five bullets and description; Keyword produces visible search terms and Amazon backend keywords; Attribute maps the product to the category schema; Image checks the photos. A fifth, the Correction Agent, rewrites only fields that failed validation. None of them can publish anything. |

## End to end

| # | Step | What happens | Who or what does it |
|---|---|---|---|
| 1 | 1. Brand signs in | A user from a customer brand logs in and gets a signed token that carries their user id, their tenant id and their role. Every database query from then on is automatically filtered to that tenant, so one brand can never see another's data. | Human (Owner / Operator / Reviewer / Admin) + Next.js dashboard with NextAuth, FastAPI middleware |
| 2 | 2. Connect the seller accounts | The brand clicks 'connect Amazon' and/or 'connect Flipkart' and completes the marketplace's own consent flow. We store a revocable token, encrypted with AES-256 under a key held in Google Cloud KMS, in the marketplace_accounts table. The AI never sees these tokens. | Human (Admin or Owner) + OAuth flow owned by the marketplace adapters |
| 3 | 3. Import products | The brand uploads a CSV of products or types one in by hand. Each row needs at minimum a SKU, a title and one image. Images go to Google Cloud Storage. Bad rows fail individually and the rest still import - a 50-row file with 5 bad rows gives 45 products and 5 row errors, with a summary report. | Human (Operator) + Redis/ARQ background worker; the API returns a job id immediately |
| 4 | 4. Gather context and brand voice | For each product the system loads the marketplace's rules, the category's attribute template, any previous versions and any user instructions. It then searches the brand's own voice guide, glossary, compliance rules and past approved listings - stored as meaning-vectors in the brand_chunks table - and pulls the 8 closest snippets into the AI prompt. | System: load_context and retrieve_brand nodes in the LangGraph pipeline, pgvector search in PostgreSQL |
| 5 | 5. Four AI agents write the listing, all at once | Content writes the title, five bullets and the description. Keyword produces visible search terms plus Amazon's hidden backend keywords. Attribute maps the product onto the category schema. Image checks every photo - cheap pixel checks first, then a vision model. They have no dependencies on each other, so they run in parallel and the wait is the slowest one, not the sum. | AI agents (Content, Keyword, Attribute, Image) orchestrated by LangGraph, default model GPT-4o-mini |
| 6 | 6. Merge into one neutral listing | The four outputs are merged into a single Canonical Listing JSON - title, bullets, description, keywords, attributes, images, confidence score. It deliberately contains no Amazon or Flipkart field names. That is what makes adding a third marketplace later a new adapter rather than a rewrite. | System: merge_outputs node |
| 7 | 7. Deterministic validation (the safety gate) | Three checks in fixed order: does the JSON match the schema, then the Rules Engine - plain code, never an AI - checks character limits (Amazon title 200, Flipkart 150), banned terms like 'best' and 'guaranteed', missing attributes and category compliance; then the vision model judges background colour, resolution, text overlays, watermarks and whether the product fills at least 85% of the frame. | System: Rules Engine (hand-written code) + GPT-4o-mini vision, in the Control layer |
| 8 | 8. Self-correct up to three times, then stop | If something fails, a correction agent rewrites only the broken field and leaves everything else untouched, then revalidates. After three attempts the listing goes to BLOCKED with the exact list of what failed, and a human has to take over. Every failure - rules, image or marketplace - comes back in one standard ErrorReport shape. | AI correction agent, bounded by the Control layer; BLOCKED jobs land on a human work queue |
| 9 | 9. A second human approves | The pipeline deliberately pauses and saves its state to disk. A reviewer sees both marketplace versions side by side with the errors, warnings and a confidence score, and can Approve, Reject with feedback, Edit inline or Regenerate with instructions. The database itself blocks the person who generated the listing from approving it. | Human (Reviewer or Owner), enforced by a CHECK constraint on listing_versions plus an API check |
| 10 | 10. Adapters publish, field by field, and data flows back | Only now does the Amazon or Flipkart adapter translate the canonical listing into that marketplace's own field names and call its API with an idempotency key so a retry cannot create a duplicate. It reports success or failure per field, and only failed fields are retried. Approved listings are fed back into the brand's voice store, and performance data feeds later optimisation. | System: marketplace adapters in the Execution layer, driven by a Redis/ARQ publish queue |

## The four layers

| Layer | Plain English | Technology |
|---|---|---|
| Experience - what people see and click | The web dashboard where a brand signs in, connects its seller accounts, uploads a CSV, watches job progress, and where a reviewer approves or rejects listings side by side. It is strictly forbidden from calling a marketplace API itself - it only talks to our own API. | Next.js 14 (App Router) on Cloud Run, NextAuth issuing JWTs, TypeScript types generated from the shared Pydantic schemas in packages/schemas |
| Intelligence - the AI that writes | The generation pipeline: gather context, pull the brand's voice snippets, run four specialist agents in parallel to write copy, keywords, attributes and check images, then merge their output into one neutral listing. It proposes; it decides nothing. | LangGraph orchestrating an 11-node graph, GPT-4o-mini by default (GPT-4o as escalation, DeepSeek-V3 as budget text option), text-embedding-3-small for retrieval, ModelRouter choosing per task, every call logged as an AgentRun |
| Control - what decides whether it may go live | The safety half. A three-stage validation pipeline (JSON schema, then the deterministic Rules Engine, then vision checks on images), one standard error format, at most three AI self-corrections, then the mandatory human approval gate with maker-checker and an append-only audit log. | FastAPI (~10 endpoints), a hand-written Rules Engine with rules stored as data in the rules table, Pydantic validation, GPT-4o-mini vision for image judgement, PostgreSQL CHECK constraint on listing_versions |
| Execution - what actually talks to Amazon and Flipkart | The adapters. Each one translates the neutral listing into that marketplace's real field names, owns its own login and token refresh, its own rate limits and error codes, and reports success or failure field by field rather than as a single yes or no. | Amazon SP-API adapter (OAuth via Login with Amazon) and Flipkart Seller API adapter, driven by Redis + ARQ publish workers, with idempotency keys and exponential backoff (1s doubling to a 60s cap, with jitter) |

## In the MVP

- Exactly two marketplaces: Amazon (SP-API) and Flipkart (Seller API)
- Two import routes: CSV bulk upload (async, row-level validation, bad rows isolated) and manual single-product entry. Each row needs a SKU, a title and at least one image.
- Four generation agents running in parallel: Content (title, 5 bullets, description), Keyword (search terms plus Amazon's 250-byte backend keywords), Attribute (structured category attributes), Image (compliance checks)
- Brand voice by retrieval: brand guide, style rules, glossary, compliance rules and past approved listings chunked into brand_chunks and searched with pgvector, top 8 snippets into the prompt
- The deterministic Rules Engine: character limits, banned terms, required attributes, category compliance - rules stored as data so changes need no deployment
- Image checks in two stages: free Pillow pixel checks first (resolution, format, aspect ratio, file size, corruption, EXIF), then GPT-4o-mini vision for background, fill ratio, text overlays and watermarks, producing an ImageReport
- One unified ErrorReport shape plus bounded self-correction: at most 3 attempts on the failed field, then BLOCKED
- Mandatory human approval with maker-checker enforced in the database, side-by-side marketplace comparison, and Approve / Reject with feedback / Edit inline / Regenerate
- Publishing through a Redis queue with idempotency keys and per-field success or failure, retrying only failed fields
- Security baseline: JWT login, four roles, tenant-scoped repositories, KMS-encrypted marketplace tokens, append-only audit log
- Observability baseline: Sentry, OpenTelemetry/Cloud Trace, structured logs with correlation IDs, and per-call AgentRun cost telemetry
- Basic analytics (contents of the screen are not actually specified anywhere - pin this down with the client)

## Explicitly out of the MVP

- Any marketplace other than Amazon and Flipkart. Myntra, Ajio, Meesho and Nykaa are named as future adapters. Shopify, Walmart and TikTok Shop appear nowhere in the 233 specs at all - correct that expectation before anyone commits to it.
- Auto-publishing of any kind. Confidence-based auto-publish is Phase 3 and depends on a confidence formula that does not yet exist.
- The Optimization Agent, competitive signals, the AI learning loop and advanced model routing - all Phase 3.
- Version history with a comparison UI, and cross-channel sales attribution - both Phase 2, and both are commonly assumed to be in the MVP. They are not.
- The unified analytics dashboard, webhooks, prompt versioning and A/B testing, and the AI quality metrics themselves (JSON validity rate, first-pass validation rate, approval rate, average retries, cost per listing) - all Phase 2. At launch you record the raw numbers but nothing computes or alerts on the ratios.
- Catalog sync with ERP or PIM systems. One spec lists it as an MVP import method; the import spec marks it Future. Rule it out in writing before a demo promises Shopify-style sync.
- API versioning. There is no /v1/ prefix in the MVP, so a breaking change has no compatibility escape hatch. Tell the front end to build polling only, despite one spec mentioning webhook callbacks.
- SOC 2, ISO certification and enterprise SAML/SSO. You cannot sell to a buyer that requires them.
- The eight future agents - Pricing, Ads, Inventory, Returns, Revenue Ops, Catalog, Ranking and the expanded Optimization agent - and the Agent Registry that would manage them. The architecture diagram a client may have seen shows nine agents; exactly one, the Listing Agent, is being built.
- Named non-goals: no model training or fine-tuning, no Kubernetes, no Kafka, no separate vector database, no competitor scraping.
- Any tooling that fixes images. The AI cannot self-correct a photo - the user must fix and re-upload. The ImageReport suggests things like 'use background removal' but no such capability is in scope.

## Glossary

| Term | Means |
|---|---|
| AI Marketplace OS | The product. Software that drafts and publishes product listings on Amazon and Flipkart for brands, with AI writing and a human approving. |
| D2C | Direct-to-consumer brand - a brand that sells its own products online rather than through distributors. This is the paying customer. |
| Listing | The product page content: title, five bullet points, description, search keywords, structured attributes and images. |
| Canonical Listing JSON | One master, marketplace-neutral version of a listing. It contains no Amazon or Flipkart field names - adapters translate it on the way out. This is the contract that holds the whole system together. |
| Adapter | A plug-in module holding everything specific to one marketplace: its API, its login, its rate limits, its field names. Adding Myntra later means writing one adapter, not changing the core. |
| SP-API | Amazon's Selling Partner API - the official interface for creating seller listings. Access needs a developer application that Amazon must approve, typically 2-4 weeks. |
| Rules Engine | Hand-written, non-AI code that checks character limits, banned words and required attributes. It gives the same answer every time and it is the final gate before anything publishes. It is never allowed to be an AI call. |
| Tenant / tenant isolation | One customer brand. Every database row carries a tenant id and every query filters on it, so one brand can never read another's data. This is the highest-severity control in the system. |
| Maker-checker | The rule that whoever generated a listing cannot be the one who approves it. Enforced by an actual database constraint, not just in the app. It means every customer needs at least two human accounts to go live. |
| RAG / brand voice retrieval | Retrieval-Augmented Generation. Before writing, the system looks up the brand's own voice guide, glossary and past approved copy and pastes the 8 most relevant snippets into the AI prompt. Brand voice is retrieved, never trained. |
| pgvector / HNSW index | A PostgreSQL add-on that stores those brand snippets as 1,536-number meaning-vectors in the same database. No separate vector database to operate. HNSW is the index type that makes the search fast - target under 50ms. |
| LangGraph | The library that runs the generation pipeline as a flowchart of 11 named steps, runs the four agents in parallel, and can pause mid-run for human approval and resume days later. |
| AgentRun | A database row written for every single AI call, recording model, tokens in and out, cost, duration and status. This is how AI spend is measured - cost per listing is queryable from day one. |
| ErrorReport | One fixed JSON error format used by rules failures, image failures and marketplace rejections alike: field, error code, message, value submitted, suggestion, source, marketplace. Support, the UI, the retry logic and the correction AI all read the same shape. |
| BLOCKED | The dead-end state after three failed AI correction attempts. A human must edit and resubmit. The BLOCKED rate is your key operational metric - those listings cost AI spend and still need a person. |
| Partial publish / PARTIALLY_PUBLISHED | A publish where some fields went live and some did not. 'The listing failed' is never the right status report - always ask which fields failed and what the marketplace's raw error said. |
| Idempotency key | A unique tag on every marketplace write so that retrying the same call cannot create a duplicate listing. |
| Redis / ARQ | Redis is the queue and cache; ARQ is the Python worker library that pulls jobs off it. Anything slow - CSV import, generation, publishing - returns a job id immediately and runs in the background. |
| Confidence score | A 0-to-1 number shown to reviewers to prioritise attention. It never lets a listing skip the deterministic rules. Nobody has defined what a good score is - treat it as decorative until they do. |
| Cloud Run / Cloud SQL / Memorystore / GCS | Google Cloud building blocks: serverless containers for the apps, managed PostgreSQL 16 for data, managed Redis for the queue, object storage for images and CSVs. |

## Running cost

"Running cost is small and boring; AI spend is the one that moves. Infrastructure on Google Cloud is roughly $150-250 a month (about INR 13,000-22,000) for Cloud SQL db-custom-1-3840, Memorystore Basic 1GB Redis, pay-per-use Cloud Run and GCS - and that figure explicitly excludes AI tokens. Amazon SP-API and Flipkart Seller API are free for authorised sellers, so there is no per-transaction fee to model. The variable cost is roughly INR 1-3 per listing generated (about 2,000-4,000 input tokens and 500-1,000 output tokens, with the Content Agent the biggest spender) plus INR 0.5-1 per image compliance check, against a tracked target of INR 3-8 total per successful listing; embeddings are under INR 100 a month. Image volume, not listing count, is what causes cost surprises. Three warnings. First, there are two contradictory baselines in the docs: an older AWS section quoting INR 8,000-11,000 a month all-in including AI, and the newer GCP build plan quoting $150-250 excluding AI - roughly 2-3x apart, so find out which number the approved business case used. Second, nothing in the docs sets an LLM budget, a spend cap or an alert threshold; the only stated mitigation is 'route to the cheapest model and log cost in agent_runs'. Third, the cost that will creep is the escalation rate to full GPT-4o after JSON validation failures - it shows up as a quiet invoice increase, not an error, and no alert on it exists. The good news: agent_runs records model, tokens in and out, cost and duration for every call from Day 10, so cost per listing, per tenant and per model is a query, not a rebuild. Staging is never costed anywhere - budget another 40-60% of production. And neither the four engineers' salaries nor any LLM figure appears in the plan at all."


## Source

Full 233-spec architecture: https://system-arch.vercel.app (machine-readable at `/llms-full.txt` and `/content.json`).
