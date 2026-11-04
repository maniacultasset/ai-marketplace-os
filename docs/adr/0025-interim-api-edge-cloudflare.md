# ADR-0025: Interim API edge on Cloudflare Free, with an origin secret checked by the app

- **Status:** proposed (EDGE-CF, 2026-11-03)
- **Date:** 2026-11-03
- **Deciders:** Tech Lead (the user); the client for the new processor (decision 2)
- **Amends:** ADR-0022 until Cloud Armor quota returns. ADR-0022 stays the target design; its
  DPP 1.1.1 gate (ADR-0022 "DPP 1.1.1 gate and endpoint inventory") is replaced by the interim
  gate below while this ADR is in force.

## Context

Amazon auto-rejected the SP-API developer profile on 3 Nov 2026 for "No" answers, among them the
network-security question (firewalls, IDS/IPS, anti-malware, segmentation). ADR-0022 answers it
with Cloud Armor on a global external load balancer, but the Cloud Armor quota on `aimos-prod` is
0 and cannot be raised for 2-3 weeks. The API runs on Cloud Run with `--ingress=all`.

Cloudflare Free gives the Free Managed Ruleset, unmetered L3-L7 DDoS mitigation, one rate limiting
rule, five custom rules and a zone minimum TLS version. It cannot proxy straight to `*.run.app`:
the Google front end routes by Host, and a Host override is Enterprise-only. Cloud Run ingress
cannot filter by source IP, and `internal-and-cloud-load-balancing` would also refuse Cloudflare.

## Decision

1. **Edge.** `api.kuinn.in` is a Cloudflare Worker Custom Domain on the Free plan
   (`scripts/deploy/cloudflare/api-edge-worker.js`). The Worker forwards each request to the
   run.app URL (`ORIGIN_URL`), only for its own host (`EDGE_HOST`), on a target built from the fixed
   origin (never resolved against it), with `redirect: "manual"`. Zone settings: SSL Full (strict),
   minimum TLS 1.2, TLS 1.3 on, Always Use HTTPS, the Free Managed Ruleset, method and path
   allow-list custom rules. Checklist: `docs/runbooks/EDGE-CLOUDFLARE.md`.
2. **Processor.** Cloudflare becomes a processor of API traffic (TLS ends at its edge). The Worker
   source is pasted into the Cloudflare dashboard: a recorded exception to RULES §9 "project code
   never goes to third-party services", limited to that one reviewed file.
3. **Workers plan.** Workers Free (100,000 requests a day, fail closed past it). The owner moves to
   Workers Paid ($5 a month) if the API passes about 50,000 requests a day.
4. **Origin secret.** The Worker adds `x-kuinn-origin-auth: <EDGE_ORIGIN_SECRET>`. The app
   (`app/api/origin_auth.py`) removes that header from every request before anything else sees it
   (Sentry's patched layer included) and compares it with `hmac.compare_digest` against every
   configured value. Under `ORIGIN_AUTH=enforce` any request without a match gets 403
   `PERMISSION_DENIED` before routing, rate limits, the body cap or any body read. `/healthz` and
   `/readyz` are exempt (exact paths). `off` never refuses.
5. **Names.** `ORIGIN_AUTH` (off|enforce), `EDGE_ORIGIN_SECRET` (Secret Manager, api only),
   `EDGE_PROVIDER` (blank|armor|cloudflare, a toolkit key in `prod.env`), headers
   `x-kuinn-origin-auth` and `x-kuinn-client-ip`, scope key `mkos.origin_ok`, Worker vars
   `ORIGIN_URL` and `EDGE_HOST`, Worker secret `ORIGIN_SECRET`.
6. **Rotation.** `EDGE_ORIGIN_SECRET` may hold two values separated by one space (current, then
   previous) for one rotation window. Each value is printable ASCII, no whitespace, 32+ characters;
   `enforce` with anything else refuses boot.
7. **Client IP.** Through the Worker, `X-Forwarded-For` ends with a Cloudflare address, so the
   Worker sends `cf-connecting-ip` as `x-kuinn-client-ip`. The limiter trusts it only on a request
   whose origin secret matched (in `off` too); otherwise the header is stripped and the
   `HOPS` logic applies as before. `HOPS` does not change.
8. **Toolkit.** `deploy.sh` with `EDGE_PROVIDER=cloudflare` binds the secret to the api, keeps
   `--ingress=all`, never enables the load balancer APIs and refuses the load balancer `edge`
   subcommands. Under `enforce` a deploy smokes the tagged revision for 403 without the header and
   401 with the Secret Manager value before the traffic shift, then the edge for 401.
9. **TLS.** Minimum TLS 1.2, as ADR-0022 decision 2 (R06584 "TLS 1.3 everywhere" stays deferred,
   T133).
10. **Rate rule.** One Cloudflare rule, added only once the SPA calls `api.kuinn.in` directly: POST
    on the login, MFA and password paths, 20 per 10 s per IP, block 10 s. The app's own per-account
    limits stay the main control.

## Interim DPP 1.1.1 gate (replaces ADR-0022's while this ADR is in force)

DPP 1.1.1 is answered "yes" only once: the live legs L1 (edge 401 with `cf-ray`, TLS 1.0/1.1
refused), L2 (run.app 403 under `enforce`), L3 (a Free Managed Ruleset test request and a custom
rule block show in Security Events) and L7 (account hygiene) have passed; the Free Managed Ruleset
is on; and the custom rules are enforced. The answer never claims OWASP CRS, IDS sensors or upload
malware scanning.

## Consequences

- $0 a month on the default path ($5 if traffic passes the Workers Free cap). The half-made static
  IP from the ADR-0022 run bills about $7.30 a month until the owner releases it.
- Someone holding the secret bypasses the WAF (not auth, MFA or per-account limits) and can pick
  their limiter IP; rotate on suspicion.
- The Cloudflare account holds all `kuinn.in` DNS after the nameserver change, so it and GoDaddy
  need 2FA, named members only, domain lock and no Global API Key before the change.
- One extra hop (Cloudflare edge in India to asia-south1). The Free ruleset is narrower than the
  ADR-0022 Armor rules.
- When Cloud Armor quota returns, ADR-0022 runs as written (`EDGE_PROVIDER` blank or `armor`) and
  this ADR is superseded.
