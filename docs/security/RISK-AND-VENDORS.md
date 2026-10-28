# Risk assessment, vendors and organisational changes

Amazon DPP 1.6.1, 2.8 and 3; AUP 3.11, 4.7 and 4.9. Owner: the engineering lead. Approved by: the
client director (senior management) every year. First assessment: 2026-10-28, approval pending.

## Yearly risk assessment (DPP 1.6.1)

Likelihood and impact are 1 (low) to 3 (high); risk = likelihood x impact.

| # | Threat | L | I | Risk | Treatment | Residual and owner |
|---|---|---|---|---|---|---|
| R1 | Theft of marketplace tokens from the database | 1 | 3 | 3 | Tokens sealed with KMS envelope encryption; the api may only encrypt, the worker decrypts (ADR-0018); forced RLS; no public DB network | Low. Engineering |
| R2 | Account takeover of a Kuinn user | 2 | 3 | 6 | Password rules, lockout at 10 failures, TOTP MFA (ADR-0021) | Medium until `MFA_ENFORCEMENT=all`, then low. Engineering + dashboard team |
| R3 | Takeover of a staff cloud account (GCP, GitHub, Vercel) | 2 | 3 | 6 | MFA and a password manager on every staff account; one human GCP owner; quarterly review (`ACCESS-MANAGEMENT.md`) | Medium until the owner confirms MFA and the org 2FA rule. Owner |
| R4 | Web attack on the API (injection, abuse, DoS) | 2 | 2 | 4 | Parameterised SQL, schema validation, rate limits; Cloud Armor WAF and Adaptive Protection (ADR-0022) | Medium until the edge is live after DNS, then low. Engineering |
| R5 | Cross-tenant data exposure by a code bug | 1 | 3 | 3 | Forced RLS on every tenant table, tenant-isolation tests (`make tenant-isolation`), import-linter contracts | Low. Engineering |
| R6 | Vulnerable dependency | 2 | 2 | 4 | pip-audit before every merge, monthly scan, deadlines (`VULNERABILITY-MANAGEMENT.md`) | Medium until container scanning is on and the dashboard findings are fixed. Engineering + owner |
| R7 | Secret leaked in code | 1 | 3 | 3 | gitleaks on every commit and over all history; Secret Manager only | Low. Engineering |
| R8 | Data loss (bad migration, region outage) | 1 | 3 | 3 | Daily backups, 7-day PITR, restore runbook | Medium until a second backup region is agreed. Client + owner |
| R9 | Undetected incident | 2 | 3 | 6 | Security logs 400 days, fortnightly review, edge alerts (`INCIDENT-RESPONSE.md`) | Medium until `setup` and `edge alerts` run and reviews start. Owner |
| R10 | Vendor breach (hosting, error tracking, AI provider) | 1 | 2 | 2 | Least data to each vendor (table below); no marketplace data to AI providers | Low. Engineering |
| R11 | Lost or stolen laptop | 2 | 2 | 4 | Device standard (`ACCESS-MANAGEMENT.md`) | Medium until devices are inventoried. Owner + client |
| R12 | Misuse of Kuinn by a brand against marketplace rules | 1 | 2 | 2 | Human approval of every listing (ADR-0008), rule checks before publish, abuse reporting (AUP 1.3) | Low. Client |

Approval:

| Date | Approved by (name, role) | Next review |
|---|---|---|
| | | 2027-10-28 |

## Sub-processors (DPP 2.8, AUP 4.7)

Assess each vendor before it gets access to marketplace data, and again every year. The check:
its security certifications (SOC 2 or ISO 27001), its data-processing terms, where it stores data,
what Kuinn sends it, and whether its standards are at least as strict as ours (AUP 4.7).

| Vendor | Purpose | Marketplace data it holds | Other data it holds | Region | Assessed |
|---|---|---|---|---|---|
| Google Cloud | Hosting: Cloud Run, Cloud SQL, Cloud Storage, KMS, Secret Manager, Logging | Yes: tokens (sealed), listing state, raw answers | All application data | asia-south1 (Mumbai); logging buckets global | 2026-10-28, first pass: ISO 27001 and SOC 2 per Google's compliance pages (to confirm and file the reports) |
| Vercel | Hosts the dashboard's static files | None (the browser calls the API directly) | None beyond access logs | Global edge | Pending |
| Sentry | Error tracking for the api | None by design: events pass the redaction filter, `send_default_pii=False` (`apps/api/app/core/observability.py`) | Stack traces with ids | Per the Sentry org settings (to confirm) | Pending |
| OpenAI | AI drafting of listing text and image checks | None: generation reads no marketplace account or answer | The brand's product data and brand documents | US (to confirm) | Pending |
| DeepSeek (only if `LLM_PROVIDER=deepseek`) | AI drafting | None | The brand's product data | To confirm before it is enabled | Pending; not enabled in prod |
| GitHub | Source code | None | Code only; no secrets (gitleaks) | US | Pending |
| Amazon, Flipkart | The marketplaces themselves | They are the source | - | - | Not a sub-processor |

Kuinn tells brands which vendors process their data and why in the public privacy policy (AUP
4.9). Any new vendor is added here and to the privacy policy before it receives data.

## Organisational changes (AUP 3.11)

Policy: Kuinn informs Amazon SP-API Solution Provider Support
(https://developer.amazonservices.com/support) within 30 days of any merger, acquisition, change of
ownership, or material change to the Kuinn product that changes its need for or use of Amazon
data, and discloses any affiliated entity involved when it requests additional roles (AUP 3.12).
The same applies to Flipkart through an API GA ticket. The client director owns this; the
engineering lead raises it when a planned product change adds an API role or a new data use.

## Records (DPP 3)

Keep this folder's logs (scans, reviews, incidents, access register, training, risk approvals),
the pentest reports and the vendor assessments for the whole time Kuinn uses the Amazon and
Flipkart APIs and for 12 months after. Git history is the record of each document's versions; the
pentest reports and vendor certificates are kept in the company's private document store, listed
here by title and date.

| Date | Record | Where |
|---|---|---|
| | | |
