# ADR-0022: API edge: Cloud Armor WAF, locked ingress, candidate smoke through the load balancer

- **Status:** proposed (DPP-2, 2026-10-27)
- **Date:** 2026-10-27
- **Deciders:** Tech Lead (the user)
- **Amends:** ADR-0001 deploy step "smoke test on its tagged URL" (line 39): once ingress is locked,
  the zero-traffic revision is smoked through the load balancer's `candidate` header route.

## Context

Amazon's Data Protection Policy asks for application-layer protection (a WAF or equivalent) on
every internet-facing endpoint (DPP 1.1.1), evidence that it is deployed and managed (1.1.3), and
TLS 1.2 or higher (1.5). The prod API runs on Cloud Run with `--allow-unauthenticated
--ingress=all`, no load balancer and no Cloud Armor. ADR-0001 already names the global external
Application Load Balancer with a serverless NEG as "the place to add Cloud Armor later", and its
fallback "a security review requires a WAF: add Cloud Armor on the existing load balancer".
The default SSL policy of a load balancer is COMPATIBLE with minimum TLS 1.0, which would break
DPP 1.5.

## Decision

1. **Option A:** a global external Application Load Balancer for `api.kuinn.in` (Google-managed
   certificate, :443 only, no :80 listener), two serverless NEGs to `aimos-api` in asia-south1
   (the service, and its `candidate` tag), two `EXTERNAL_MANAGED` backend services with request
   logging at 100 %, both carrying the Cloud Armor Standard policy `kuinn-api-waf`
   (`--json-parsing=STANDARD`, `--log-level=NORMAL`, Layer 7 DDoS defense). A URL map sends the
   header `x-aimos-candidate: <value>` to the candidate backend and everything else to the main
   one; the value is 128 random bits written by each `edge setup` run (decision 8).
2. **TLS:** SSL policy MODERN with minimum TLS 1.2 (meets DPP 1.5). RESTRICTED with minimum 1.3
   is possible (a 1.3 minimum must use the RESTRICTED profile) and would meet R06584 ("TLS 1.3
   enforced everywhere"), but drops TLS 1.2 clients; TEAM-QUESTIONS T133.
3. **Rules as code:** `scripts/deploy/gcp/armor-rules.yaml`, converged rule by rule by
   `deploy.sh prod edge rules` (`security-policies import` cannot update an existing policy's
   rules), with a post-apply export check. Every rule starts in preview and leaves it only when
   its group is listed in `EDGE_ENFORCE`: `path`, then `rate`, then the CRS groups by tier.
4. **Rule order (changes the proposal's numbering, not its intent):** Cloud Armor applies the
   first matching rule, and a throttle or rate-based-ban rule whose conform action is allow lets
   the request through. So the seven OWASP CRS 4.22 sets sit at priorities 200-260, in enforce
   order, before every rate rule (300+); the two auth rules (throttle and ban) are one
   rate-based-ban at 300.
5. **No CRS method enforcement.** `methodenforcement-v422-stable` allows only GET, HEAD, POST and
   OPTIONS, and the API serves PATCH and DELETE. Rule 110 is a method allow-list of what the API
   serves plus HEAD and OPTIONS.
6. **CRS skip:** every CRS set skips POSTs to the three multipart upload routes and to the auth
   paths whose JSON body carries a password, a TOTP/recovery code or a refresh token, logout
   included (Armor exclusions cannot target JSON body fields). Other methods on those paths stay
   CRS-inspected. Rate rules and the skip match the decoded path. `edge rules` compares every
   exclusion kind (headers, cookies, query parameters, URIs, rule-id scope), so an exclusion added
   out of band is drift.
7. **Locked ingress:** after the cutover, Cloud Run ingress is `internal-and-cloud-load-balancing`,
   set from `INGRESS` in `prod.env` so a redeploy keeps it. `edge lock-ingress` refuses until the
   SPA calls the edge, removes stale `sha-*` tags, probes run.app and every tag URL after locking,
   and unlocks again by itself if any still answers. `deploy` refuses to unlock a locked service
   or to change the live `JWT_ISSUER` (pinnable with `JWT_ISSUER`).
8. **Candidate smoke through the load balancer:** with ingress locked, a deploy tags the
   zero-traffic revision `candidate`, smokes `https://api.kuinn.in` with the candidate header,
   then shifts traffic. The header route matches only a random value: `edge setup` writes 128
   random bits into the URL map on each run (rotation is a rerun), and a deploy reads it back
   with `url-maps describe` and never prints it. The `candidate` tag lives only for the deploy:
   it is removed after the shift, after a failed smoke and on any other exit (an EXIT trap,
   including Ctrl-C), so between deploys the candidate backend has no target. Only someone who
   can read the URL map (project viewers) can reach an unsmoked revision, and only while a deploy
   runs; the public cannot use the second backend's separate Armor rate counters to double the
   auth throttle. A per-deploy value written into the URL map was rejected: a URL map change takes
   time to propagate, and until it does the header falls through to the live revision, so the
   smoke could pass against the wrong revision. A plain deploy never moves a live open service to
   the locked ingress: that is `edge lock-ingress` (`DEPLOY_ALLOW_LOCK=1` overrides).
9. **Logging and alerts:** a 400-day `security-logs` bucket fed by a sink of Armor denies (preview
   and enforced), auth/signup failures and Adaptive Protection alerts, never OAuth callback URLs;
   alert policies for deny spikes, 429 spikes, load balancer 5xx, a failing uptime check and
   Adaptive Protection, to one email channel. Cloud Audit admin activity is already kept 400 days
   in `_Required`.

## Accepted risks

- Project members who can read the URL map can reach the revision being smoked during a deploy
  (with its own Armor rate counters); the public cannot, since the route matches only a random
  value, and the tag is removed when the deploy ends.
- Between the deploy that changes `HOPS` and the lock, a direct run.app caller could pick its app
  rate-limit bucket with a forged `X-Forwarded-For`; EDGE-CUTOVER steps 5, 8 and 9 bound this to
  one cutover window.
- During the 48-hour rewrite grace, users on the Vercel-rewrite path may share one rate-limit
  bucket (the Vercel egress IP).
- `edge rules` is not atomic: a failure mid-run leaves a mixed policy until a rerun converges it.
- CRS runs before the rate rules, so a CRS-denied request is never counted by rules 300/400 and a
  WAF-bypass fuzzer is not banned by them; the deny-spike alert covers sustained deny traffic.
- The upload routes and the secret-bearing auth paths skip CRS; app validation covers them.
- Armor's preconfigured rules inspect only the first 64 kB of a body; the app accepts up to
  10 MiB.
- Armor's own 403/404/429 carry no CORS header, so the browser sees them as network errors; the
  frontend shows "blocked or limited" when `/readyz` still answers.

## DPP 1.1.1 gate and endpoint inventory

DPP 1.1.1 is answered "yes" only once every group, sqli and xss included, is enforced and
`edge status` prints `preview rules left: 0`; until then "WAF deployed, in tuning (preview)". The
endpoint inventory (API on the edge, run.app refused, SPA behind the Vercel firewall on Pro, GCS
signed URLs with bucket CORS, Cloud SQL through the connector only) is kept in
`docs/runbooks/EDGE-CUTOVER.md`.

## Consequences

- Prod gains about $40 a month (load balancer, Cloud Armor Standard, the security bucket).
- Checkouts older than DPP-2 hard-code `--ingress=all`; every lane that can deploy prod is rebased
  or retired before the lock (EDGE-CUTOVER step 0).
- Staging keeps run.app with no edge (no `EDGE_DOMAIN`).
