# Incident response plan

Amazon DPP 1.6.2-1.6.5 and 2.6. Owner of this plan: the engineering lead. Approved by: the client
director (record below). Review it every six months and after any major change to the system, its
controls, its hosting, its risk level or its vendors.

A **Security Incident** (DPP section 4) is any actual or suspected unauthorized access, collection,
acquisition, use, transmission, disclosure, corruption or loss of marketplace data, or a breach of
any environment that holds it. When in doubt, treat it as one: the 24-hour clock starts at
detection, not at confirmation.

## Contacts

| Role | Who | Reach |
|---|---|---|
| Incident Management Point of Contact (IMPOC), DPP 1.6.5 | To be named by Maniacult | To be filled (a monitored company mailbox, not a personal address) |
| Backup IMPOC | To be named by Maniacult | To be filled |
| Incident lead (technical) | Engineering lead | Team chat, then phone |
| GCP project owner | The `aimos-prod` owner account | Can disable accounts, rotate keys, lock ingress |
| Amazon | Security team | security@amazon.com (Security Incidents); spapi-abuse@amazon.com (seller abuse, AUP 1.3) |
| Flipkart | Seller API support | A ticket under the "API GA" node in the partner portal |
| India CERT-In | Incident reporting | incident@cert-in.org.in; check the current reporting window in the CERT-In directions before relying on it |

Keep the phone numbers in the company's private contact list, not in this repo.

## Roles

- **IMPOC:** owns the outside communication: Amazon, Flipkart, regulators, affected brands. Only
  the IMPOC (or the director) speaks for the company. Never speak for Amazon (DPP 1.6.3).
- **Incident lead:** runs containment and the investigation, keeps the timeline.
- **Scribe:** anyone on the team; writes every action with its UTC time in the incident record.

## Incident types

| # | Type | Examples | First containment |
|---|---|---|---|
| 1 | Marketplace token exposure | A token, client secret or KMS permission leaked; a sealed token readable by the wrong role | Revoke the connection (`POST /api/v1/marketplaces/accounts/{id}/revoke`), rotate the LWA or Flipkart client secret in the developer portal and Secret Manager, ask affected brands to remove Kuinn in Seller Central and reconnect |
| 2 | Account takeover | A Kuinn user or staff login used by someone else; repeated `auth.locked` for one account | `user reset-mfa` and a new password (`docs/runbooks/MFA-STAFF.md`); deactivate the membership; for GCP, GitHub or Vercel, the owner suspends the account and removes its access |
| 3 | Cross-tenant data access | A brand sees another brand's data | Turn off the affected route or feature by a release; preserve logs and the database state (PITR clone) before any fix |
| 4 | Secret or code leak | A secret in git, a public bucket, a leaked laptop | Rotate the secret (Secret Manager new version, redeploy), `gitleaks` over all history, revoke the device's sessions |
| 5 | Attack on the API | WAF deny spike, 429 spike, unusual data volume, Adaptive Protection alert | Enforce the Armor rules in preview (`deploy.sh prod edge enforce`), add a deny rule for the source, lock ingress |
| 6 | Seller misuse (not a security incident by itself) | A brand uses Kuinn to break its Amazon agreement | Block the brand's access and email spapi-abuse@amazon.com (AUP 1.3) |
| 7 | Vendor breach | GCP, Vercel, Sentry or an AI provider reports a breach | Rotate credentials held by that vendor; assess what data the vendor had (`RISK-AND-VENDORS.md`) |
| 8 | Data loss or corruption | Bad migration, deleted rows, region outage | Restore by PITR clone (`docs/runbooks/DEPLOY-GCP.md`, "Restore the database"); see `DATA-RETENTION.md` |

## Steps

1. **Detect and log.** Sources: alerts to `ALERT_EMAIL` (`deploy.sh edge alerts`), Sentry, the
   fortnightly log review, a brand, Amazon or Flipkart, a scan. Open an incident record (template
   below) and note the detection time in UTC.
2. **Assess within 1 hour.** Is marketplace data, a credential or a brand account involved? If yes or
   unsure, it is a Security Incident. Call the IMPOC.
3. **Contain.** Use the first containment for the type above. Prefer actions that keep evidence
   (revoke, rotate, lock ingress) over actions that destroy it (deleting instances or logs).
4. **Notify Amazon within 24 hours of detection** (DPP 1.6.3) when the incident involves Amazon
   data, an Amazon credential or the systems that hold them: the IMPOC emails security@amazon.com
   with what happened, when it was detected, the data and sellers affected (as far as known),
   containment so far and a contact. Send what is known; do not wait for the investigation. Tell
   Flipkart through an API GA ticket for Flipkart data. Regulators (CERT-In, the Data Protection
   Board under the DPDP Act) are the company's own duty; the IMPOC decides with counsel. Brands are
   told by the IMPOC after Amazon or Flipkart are informed.
5. **Investigate** (DPP 1.6.4). Read the logs: `gcloud logging read` on `_Required` (Cloud Audit
   Logs, 400 days), `security-logs` (sign-in events, refused requests, WAF denies, 400 days) and
   `_Default` (30 days, export what you need before it expires). Read the `audit_log` table. Clone
   the database to a point in time if the data state matters.
6. **Preserve evidence** with a chain of custody: export logs and clones to a dedicated bucket in
   `aimos-prod` with object retention, record who exported what and when, and record the sha256 of
   each export in the incident record. Do not change the exported copies.
7. **Recover and close.** Fix the cause, redeploy, confirm the fix, return the WAF and ingress to
   normal. Write the root cause, the remediation and the controls added to stop a repeat. Send the
   record to Amazon on request.
8. **Learn.** Within 2 weeks: a short review, an update to this plan and to `RISK-AND-VENDORS.md`.

## Fortnightly log review (DPP 2.6.1)

Every second Monday the engineering lead reviews, and records one line in the table below:
- `security-logs`: WAF denies by rule and source, 401/403/429 counts by path, `auth.locked`,
  `auth.login_failed`, `auth.mfa_failed`, `auth.refresh_reuse_detected`, `auth.password_changed`
  and the staff events `auth.unlocked` and `auth.mfa_reset`.
- `_Required`: IAM policy changes, new service account keys, KMS and Secret Manager admin calls.
- Sentry: new error types.

Example query:

```
gcloud logging read 'jsonPayload.message=~"^auth\\.(locked|mfa_failed|refresh_reuse_detected)"' \
  --bucket=security-logs --location=global --view=_AllLogs --freshness=14d --project=aimos-prod
```

| Date | Reviewer | Findings | Follow-up |
|---|---|---|---|
| | | | |

## Incident record template

```
Incident id:            INC-YYYY-NN
Detected (UTC):         
Detected by / source:   
Type (table above):     
Data and sellers involved:
Amazon notified (UTC, by whom):         Flipkart notified:
Timeline (UTC, action, by whom):
Evidence exported (object, sha256, by whom, when):
Root cause:
Remediation:
Controls added:
Closed (UTC):
```

## Exercises and reviews

| Date | What | Outcome |
|---|---|---|
| 2026-10-28 | Plan written | First version; IMPOC to be named; approval pending |
| By 2027-01-31 | Tabletop exercise (type 1, a leaked LWA client secret) | |
| 2027-04-28 | Six-month review | |
