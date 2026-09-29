# ADR-0001: Hosting on GCP asia-south1 (Mumbai), bootstrapped profile

- **Status:** accepted (user delegated the choice to the backend architect in chat, 2026-09-27: "you
  decide the best way in terms of cost and scalability")
- **Date:** 2026-09-27
- **Deciders:** the user (Tech Lead), with the backend architect
- **Evidence:** `docs/research/hosting-2026-09.md` (6-agent research + FinOps skeptic)
- **Supersedes:** the hosting hold in AGENTS.md §4, and DECISIONS.md D10 (it reaffirms D10's cloud,
  but with a different service set)

## Context

The research ranked AWS Mumbai + Activate credits first. The skeptic judged that ranking
**conditional** on:
- (a) the client being incorporated and getting DPIIT recognition, which unlocks up to $5k of
  credits;
- (b) the team having AWS operations skills.

The user answered: **incorporation status unknown**, the user **knows GCP a little**, the year-2
budget is to be decided later, and cost plus scalability decide. With (a) unknown and (b) not met,
AWS's only edge (credits) is uncertain, and its year-2 cost is about the same as GCP's.

Since the research, ADR-0004 removed Redis and ADR-0005 removed gunicorn. On GCP that eliminates
Memorystore, and lets the worker run as a Cloud Run **worker pool** (GA 2026-04-14), with no
`$PORT` health-server workaround.

## Decision: GCP asia-south1, two projects (`aimos-staging`, `aimos-prod`); local development stays on Docker

| Component | Prod | Staging |
|---|---|---|
| API | Cloud Run service, 1 vCPU / 1 GiB, **one uvicorn process**, min 1 / max 3 | same image; min 0 / max 1 |
| Worker | **Cloud Run worker pool**, 1 vCPU / 2 GiB, 1 instance (raise to 2 by hand when needed). Runs procrastinate workers for queues `default` and `publish` in one process, with **separate concurrency** so publishing is never starved. Split into two pools later with a config change only. | 1 instance, scaled to 0 off-hours |
| Database | **Cloud SQL PostgreSQL 17, Enterprise edition** (`--database-version=POSTGRES_17 --edition=ENTERPRISE`), `db-custom-1-3840`, zonal, 30 GB SSD autoresize, automated backups + **PITR 7 days**, maintenance window outside IST business hours, `max_connections` set explicitly (100), pgvector 0.8.5. Scale to `db-custom-2-7680` on a CPU or latency alarm. | shared-core (`db-f1-micro`; `db-g1-small` if the smoke suite needs it), stopped nights and weekends |
| DB connectivity | Cloud Run's built-in Cloud SQL connection (unix socket through the managed proxy). **No VPC connector, no Direct VPC egress, no authorized networks.** Laptops use the Cloud SQL Auth Proxy. The Canon's URL + password rule is unchanged. | same |
| Object storage | GCS (images, CSVs), regional, lifecycle rules (CSVs 30 days), V4 signed URLs. Code goes through the S3-API-compatible `StorageAdapter`, the same path as local MinIO. | separate buckets |
| Secrets / keys | Secret Manager (injected as env vars; the app never calls the API). Cloud KMS keyring `marketplace-os` / key `oauth-token-key` (Canon), 90-day rotation. | own secrets and key |
| Ingress / TLS | Global external Application Load Balancer + serverless NEG for `api.<APEX>`, which is the place to add Cloud Armor later | Firebase Hosting rewrite for `api-staging.<APEX>` (about $0) |
| CI/CD | GitHub Actions, keyless via Workload Identity Federation → Artifact Registry. One image digest is promoted from staging to prod. Migrations run as a Cloud Run **job** before each deploy. | auto-deploy on merge to `main` |
| Deploys | New revision at **0% traffic** → smoke test on its tagged URL → shift traffic → watch 10–15 min → **automatic rollback** (Cloud Run revisions) | same flow |
| Observability | Cloud Logging (JSON to stdout), Cloud Trace via OTLP, Sentry | same |

**Connection ledger** (replaces Addendum §24):
- API: 3 instances × 1 process × (pool 5 + overflow 5) = 30
- Worker: 2 × (app pool 10 + procrastinate pool 5) = 30
- Migration job: 5
- Proxy and admin sessions: 10
- Reserved: 3
- **Total 78 of 100.** Recompute whenever instances or pools change.

## Estimated cost (list prices, before 18% GST; GST is reclaimable once GST-registered)

The research priced prod at $235/mo and staging at $61/mo with Memorystore and two worker pools. For
the design above:
- **Prod about $170/mo**: no Memorystore, one worker pool.
- **Staging about $25–35/mo.**
- **Total about $195–205/mo, roughly ₹16.5–17.5k.**

With 1-year committed-use discounts from year 2, about $170–180/mo.

## Credits (the client applies, in the company's name)

1. **GCP Free Trial: $300, valid 90 days.** Instant; needs a new billing account.
2. **Google for Startups Cloud Program, Start tier: $2,000, valid 12 months.**
   - Needs a live website, email on the same domain, and founding within 24 months.
   - Needs a *truthful* "plans to seek venture funding soon". Approval is discretionary.
   - The credits can't pay for OpenAI.
   - Use Zoho or a Workspace *trial* for email; paid Google Workspace voids a Start perk.
3. **If the client raises VC:** Scale tier (up to $200k over 2 yrs), or AI-first (up to $350k, but
   only if Gemini becomes core).
4. **LLM tokens: Microsoft for Startups → Azure OpenAI** (user choice: propose it to the AI team).
   $200 now, $5k after business verification.

**Incorporation** (Pvt Ltd/LLP) and GST registration are still strongly recommended. They're needed
for the Amazon SP-API developer profile, customer contracts and GST reclaim, whatever the cloud.

## Cost guardrails (day 1)

- **Budgets and quotas:**
  - Budgets with alerts at 50/80/100% per project; start at $250/mo prod and $60/mo staging.
  - An org policy that locks the region to asia-south1 (plus global services).
- **Scale limits:**
  - Explicit `max-instances` on every Cloud Run service.
  - Staging scales to zero, and its DB is stopped off-hours.
- **Housekeeping:**
  - Log retention: default buckets at 30 days prod and 7 days staging.
  - Artifact Registry and GCS lifecycle rules.
- **Spend commitments:** nothing upfront while on credits.
- **LLM:** a hard monthly cap on the LLM account, per-tenant daily budgets (Canon), and daily cost
  alerts from `agent_runs`.

## Revisit triggers

- **The client raises VC, or joins an incubator:** re-check the GCP Scale tier; AWS Activate
  Portfolio would also open up.
- **The bill goes over ₹25k/month before funding:** re-price the lean option (DigitalOcean BLR1).
- **Worker load needs autoscaling:** add worker-pool autoscaling, or split the pools.
- **A security review requires a WAF:** add Cloud Armor on the existing load balancer.

## Consequences

- The Canon's GCP rows stay, with the amendments in CANON.md: no Memorystore; the worker is a worker
  pool, not a service with `$PORT`; PG17 Enterprise; the new ledger; two projects instead of three.
- Portability rules from the research apply, so moving clouds stays a planned 1–2 engineer-week job:
  - one image;
  - plain Postgres;
  - S3-API storage adapter;
  - a KMS wrapper interface;
  - env-var secrets;
  - OTLP;
  - OpenTofu modules;
  - DNS outside the cloud.
