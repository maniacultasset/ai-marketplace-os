# Hosting and startup credits research (2026-09-27)

_A 6-agent workflow: per-provider research on credits and costs, a design pass, and a FinOps skeptic
who re-verified the numbers. **Status: research note; feeds ADR-0001.** The prices were modelled with
Redis and two always-on workers. ADR-0004 later dropped Redis, which lowers every option by roughly
$13–36/month per environment._

## Comparison (prod + staging, USD list prices, before 18% GST)

| Option | Year-1 cash after realistic credits | Year-2 per month | Credits an unfunded Indian company can realistically get | Ops burden | Score |
|---|---|---|---|---|---|
| **AWS ap-south-1 (Mumbai)**: ECS Fargate, RDS PostgreSQL, S3, KMS | **~$300–950** | ~$302 on-demand, ~$267 with a 1-yr no-upfront Savings Plan | Activate **Founders $1k** plus the **Startup India (DPIIT) route to $5k total**, plus $100–200 new-account credit. Valid 1–2 yrs. Can pay for Bedrock models, possibly including GPT on Bedrock in Mumbai (unconfirmed on an AWS India account). | medium (VPC/IAM/ECS, 400–600 lines of OpenTofu) | 8.4 |
| **Hybrid (recommended):** AWS as above, plus Microsoft for Startups credits used **only for Azure OpenAI tokens** | same | same | Microsoft: $200 now, $5k after business verification; $25k automatic milestone is **possible upside, not banked** | AWS plus one extra LLM endpoint | **8.7** |
| GCP asia-south1 (Canon): Cloud Run, Cloud SQL | ~$1,247 | ~$296, or ~$250–256 with committed-use discounts | $300 trial (90 days) + **$2k Start tier (discretionary; asks for "plans to seek venture funding soon")**. The $200k/$350k tiers need VC funding. **Credits can't pay for OpenAI.** | lowest (Cloud Run) | 7.3 |
| Azure Central India | ~$2,214 | ~$402 | $200, then $5k; $25k milestone automatic per one page, contradicted by another. Pays for Azure OpenAI. | medium | 6.1 |
| Lean: DigitalOcean BLR1 + Cloudflare R2 (+ AWS KMS) | ~$981 | **~$170** | DO $1k via Startup India (offer text dated ~2018; fragile), Neon $1k, Cloudflare $10k (DNS/CDN only) | four vendors; no native KMS; no OIDC deploys; single-node DB | 6.0 |

## Recommendation (the skeptic says it holds, **conditionally**)

**AWS Mumbai, paid for by Activate, plus Microsoft credits for Azure OpenAI tokens.** Two conditions:
- **(a)** The client company is incorporated (Pvt Ltd/LLP), or files within about 2 weeks, and
  applies for DPIIT recognition. Every credit above about $200 depends on this; so do GST input tax
  credit, the Amazon SP-API developer profile, and customer contracts.
- **(b)** At least one engineer is comfortable with ECS, IAM and Terraform/OpenTofu.

If **(a)** fails, AWS costs about $2.4k cash in year 1, and GCP (if Start is approved) or DigitalOcean
become cheaper. If **(b)** fails, stay on GCP. **Year-2 cost is effectively the same on AWS and GCP**;
DigitalOcean is about $1.2–1.6k a year cheaper but weaker on security and operations.

## Credit application plan (the client does this, in the company's name)

1. **Day 0–1:**
   - Domain.
   - A live website (product, founders, contact).
   - Business email on that domain. Use Zoho, or a Google Workspace *trial*, because paid Workspace
     voids a GCP Start perk.
   - Confirm incorporation; if there's none, file SPICe+ (7–15 days) and apply for GSTIN.
2. **Day 1–2:**
   - Open the AWS management account **in the company name** (India KYC with company PAN/COI),
     choosing the **Paid** plan.
   - Apply for **Activate Founders ($1k)** the same day: 7–10 business days. It isn't retroactive.
3. **Day 1–3:** Microsoft for Startups, if the AI team accepts Azure OpenAI.
   - A founder signs up with a **personal** Microsoft account that has never had Azure, and chooses
     "Continue without a code" ($200).
   - Complete business verification ($5k).
4. **After incorporation:**
   - DPIIT recognition (free, 2–15 working days).
   - Ask Startup India for the AWS Activate Org ID.
   - Apply for Activate Portfolio ($5k total).
5. **Track every credit** in one register (amount, expiry, account) with a 60-day expiry reminder.
6. **If the client ever joins an incubator or raises money:** Activate Portfolio via the provider
   ($10k–100k) and Microsoft's Investor offer ($100k).

## Cost guardrails (AWS)

- **Budgets and alerts:**
  - AWS Budgets on day 1: $350/mo total, alerts at 50/80/100%.
  - Cost Anomaly Detection.
  - A region-lock SCP.
- **Network:**
  - No NAT gateway: public subnets with deny-inbound security groups, plus the free S3 gateway
    endpoint.
  - No interface endpoints, no Aurora Serverless, no ElastiCache (no Redis now anyway).
- **Scale and size:**
  - One ALB per environment.
  - Staging scales to zero off-hours, and staging RDS is stopped nights and weekends.
  - RDS `db.t4g.medium`, with a CPU-credit alarm.
- **Housekeeping:**
  - Log retention 30 days prod / 7 days staging.
  - ECR and S3 lifecycle rules.
  - One customer-managed KMS key per environment.
- **Spend commitments:** nothing upfront while on credits; from year 2, 1-yr no-upfront Savings Plans.
- **LLM spend is the biggest variable cost:**
  - a hard monthly cap on the provider;
  - per-tenant daily budgets (already in the Canon);
  - daily cost alerts from `agent_runs`.

## Portability rules (stay movable)

- One image, configured by environment variables.
- Plain PostgreSQL: no Aurora, no RDS IAM auth, no `rds_*` functions.
- Object storage behind a single S3-API `StorageAdapter`, the same code path as MinIO locally.
- KMS behind a `KeyWrapper` (LocalKmsStub, AwsKms, …), with provider, key id and version stored next
  to every wrapped key.
- Secrets arrive as environment variables. The app never calls a secrets API.
- Observability through the OTel SDK/OTLP only.
- No Lambda, SQS or Step Functions in business flows; no Cognito.
- All infrastructure in OpenTofu modules.
- CI promotes one image digest and deploys over OIDC.
- DNS lives outside the cloud account.

## Watch-outs the skeptic added

- **Year-1 cash:**
  - Budget **$300–950**, because of GST (18% on AWS India invoices; whether it applies to
    credit-covered usage is unverified) and the weeks before credits land.
  - Founders followed by Startup India may give only the difference: budget $5k total.
- **Deployment mode:** if a future security review could force private subnets, build the API as a
  plain ECS service with its own internet-facing ALB, not ECS Express Mode. Express Mode on private
  subnets only creates an internal ALB, and that can't be changed later.
- **Don't** add an ADOT collector sidecar, or custom CloudWatch queue metrics. Export OTLP directly,
  and scale workers on CPU.
- **Documentation:** ADR-0001 must supersede DECISIONS.md D10 ("GCP wins… open no AWS accounts"). All
  cloud-specific Canon/ENV/BLOCKERS/config rows change in one PR before any account is opened. Moving
  from GCP to AWS is about 17 rows.
