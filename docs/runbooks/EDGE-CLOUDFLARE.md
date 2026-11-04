# Interim API edge: Cloudflare Free in front of the prod API

This runbook puts `api.kuinn.in` behind Cloudflare while the Cloud Armor quota on `aimos-prod` is
0 (ADR-0025, which amends ADR-0022 until the quota returns). A Cloudflare Worker forwards each
request to the Cloud Run `run.app` URL with the origin secret; the app refuses every other caller
once `ORIGIN_AUTH=enforce`. The load balancer path stays in `EDGE-CUTOVER.md` for later.

Every deploy command is `scripts/deploy/gcp/deploy.sh prod ...` (`DEPLOY-GCP.md`), run by the
owner. `prod.env` holds `EDGE_PROVIDER=cloudflare` and `ORIGIN_AUTH=off`; in this mode `edge setup`,
`rules`, `enforce`, `lock-ingress`, `unlock-ingress` and `alerts` refuse, and `edge status` and
`edge cors` run.

## Who does what

- **Client:** the Cloudflare account (Free) with 2FA, the owner invited by name as admin; GoDaddy
  2FA and domain lock; the GoDaddy zone export; the nameserver change; the Amazon resubmission;
  the staff-laptop anti-malware confirmation.
- **Owner:** `prod setup`, `prod release`, `edge status`; the Worker, its vars and secret; the
  Cloudflare settings; the FE Vercel env and deploys; the leftover static IP; live legs L1-L7.
- **Agents:** the backend and FE pull requests and the DPP answer wording. Never gcloud,
  wrangler, Cloudflare or GoDaddy.

## Order (each step only after the one before passed)

1. `ORIGIN_AUTH=off` release: `prod setup` (generates `EDGE_ORIGIN_SECRET`, 64 hex characters,
   granted to the api account only), then `prod release`. The api now strips the origin header and
   marks a match, but refuses nothing.
2. DNS move steps 0-3 and the Worker (below). The Worker goes live on `api.kuinn.in`.
3. L1, then L6 (per-user client IPs through the Worker).
4. The FE pull request, in three ordered deploys (`EDGE-CUTOVER.md` step 8): CSP, rewrites, then
   `VITE_API_ORIGIN=https://api.kuinn.in`. Pin `JWT_ISSUER` first (`EDGE-CUTOVER.md` step 6).
5. The Cloudflare rate rule (settings checklist, last row).
6. `enforce` release: set `ORIGIN_AUTH=enforce` in `prod.env`, commit, `prod release`. The deploy
   smokes the tagged revision for 403 without the header and 401 with the Secret Manager value
   before the traffic shift, then `https://api.kuinn.in` for 401.
7. L2-L5, then L7 evidence, then the DPP answer.

## DNS move (client and owner, no downtime)

0. **Account hygiene first (L7):** 2FA (hardware key or TOTP) on the Cloudflare and GoDaddy
   accounts; every member invited by name (no shared login); GoDaddy domain lock on; no Global API
   Key in use; any API token scoped to Workers on the `kuinn.in` zone only. Keep screenshots.
1. **Client:** add `kuinn.in` to Cloudflare (full setup). Export the GoDaddy zone file and compare
   it with Cloudflare's scan, which can miss records. Must match: apex A `216.198.79.1` and `www`
   CNAME `18b30e25d34f0887.vercel-dns-017.com` (both DNS only, grey cloud), MX
   `10 mailstore1.secureserver.net` and `0 smtp.secureserver.net`, the SPF TXT, and every other
   record in the export. DNSSEC is off (no DS record), so nothing to turn off first.
2. **Client at GoDaddy:** replace the nameservers with the two Cloudflare names exactly. Leave the
   GoDaddy zone untouched for 48 hours.
3. **Owner, once the zone is Active:** the settings checklist below.

## Cloudflare settings checklist

| Setting | Value |
|---|---|
| SSL/TLS mode | Full (strict) |
| Minimum TLS version | 1.2 |
| TLS 1.3 | on |
| Always Use HTTPS | on |
| WAF managed rules | Free Managed Ruleset on |
| Custom rule 1 (method) | host `api.kuinn.in` and method not in GET, HEAD, POST, PATCH, DELETE, OPTIONS: Block |
| Custom rule 2 (path) | host `api.kuinn.in` and path not starting with `/api/v1/` and not equal to `/readyz`: Block |
| Bot Fight Mode | off (it challenges API calls) |
| Vercel records (apex, `www`) | DNS only (grey cloud) |
| Worker `workers.dev` route | OFF |
| Worker Preview URLs | OFF |
| Rate rule (only after `VITE_API_ORIGIN` is live) | host `api.kuinn.in`, method POST, path starts with `/api/v1/auth/login`, or equals `/api/v1/auth/password`, `/api/v1/auth/password/expired` or `/api/v1/auth/mfa/step-up`; per IP, 20 requests per 10 s, Block for 10 s. Check Security Events for false hits on day one. |

## Worker deploy (owner, dashboard)

1. Workers and Pages, Create, Hello World; replace the code with
   `scripts/deploy/cloudflare/api-edge-worker.js` from the merged commit; Deploy.
2. Settings, Variables: `ORIGIN_URL` = the run.app URL (`deploy.sh prod status`), `EDGE_HOST` =
   `api.kuinn.in`.
3. Settings, Variables, Secret `ORIGIN_SECRET`: the first value of `EDGE_ORIGIN_SECRET` only, never
   the two-value version a rotation holds. Read it in your own terminal and paste it into the
   dashboard field; never into chat, a file or a ticket:
   `gcloud secrets versions access latest --secret=EDGE_ORIGIN_SECRET --project=aimos-prod | cut -d' ' -f1 | tr -d '\n' | pbcopy`,
   then clear the clipboard. Then run `deploy.sh prod edge status` and stop unless it prints
   "edge serving: yes".
4. Settings, Domains and Routes: add the Custom Domain `api.kuinn.in`; turn the `workers.dev`
   route and Preview URLs OFF.

Without any of the three settings the Worker answers 503; on any host other than `EDGE_HOST` it
answers 404. Workers Free fails closed (error 1027) past 100,000 requests a day: move to Workers
Paid if the Cloud Run request count passes about 50,000 a day.

## Live legs

- **L1:** `curl -sSI https://api.kuinn.in/api/v1/auth/me` answers 401 with a `cf-ray` header
  (`deploy.sh prod edge status` prints "edge serving: yes"); `curl --tlsv1.1 --tls-max 1.1` is
  refused. `curl https://<worker>.<account>.workers.dev/api/v1/auth/me` must not answer 401 or 403
  from the app (route off: an error, or the Worker's 404). `curl --path-as-is
  'https://api.kuinn.in//example.com/'` is blocked by the path rule or answered by the origin,
  never by example.com.
- **L2 (after `enforce`):** `curl https://<run.app URL>/api/v1/auth/me` answers 403
  (`edge status` prints "run.app direct: 403 as expected").
- **L3:** a Free Managed Ruleset test request and a request outside the path rule are blocked and
  show in Security Events (this also proves the WAF runs before the Worker). Screenshot for DPP.
- **L4:** SPA login, refresh, CSV import, image upload, Google sign-in.
- **L5:** the Amazon OAuth callback at `https://api.kuinn.in/api/v1/marketplaces/amazon/callback`
  once `AMAZON_APP_ID` is set.
- **L6:** from two known IPs, a failed login on `api.kuinn.in` lands in each caller's own limiter
  bucket. The FE move waits on L6.
- **L7:** the step 0 screenshots.

## Rotation

Work in one terminal; `NEW` and `OLD` live only in its shell variables, never in a file, and the
Worker gets `NEW` from that variable, never from the two-value version read back.

1. `NEW=$(openssl rand -hex 32)` and
   `OLD=$(gcloud secrets versions access latest --secret=EDGE_ORIGIN_SECRET --project=aimos-prod)`.
   Add `<new> <old>` (one space, no trailing newline: use `printf`, never `echo`):
   `printf '%s %s' "$NEW" "$OLD" | gcloud secrets versions add EDGE_ORIGIN_SECRET --data-file=- --project=aimos-prod`,
   then a same-image `prod deploy`: the api accepts both. A malformed value refuses the boot in
   any `ORIGIN_AUTH` mode, so the previous revision keeps serving; fix the value and deploy again.
2. Set the Worker secret `ORIGIN_SECRET` to `<new>` alone: `printf '%s' "$NEW" | pbcopy`, paste,
   clear the clipboard. Run `deploy.sh prod edge status` and stop unless it prints
   "edge serving: yes".
3. Add a version holding `<new>` alone
   (`printf '%s' "$NEW" | gcloud secrets versions add EDGE_ORIGIN_SECRET --data-file=- --project=aimos-prod`)
   and `prod deploy` again. Run `deploy.sh prod edge status` again and stop unless it prints
   "edge serving: yes". Then `unset NEW OLD`. Rotate at once on any suspicion of a leak.

## Rollback

- App: `ORIGIN_AUTH=off` in `prod.env`, commit, `prod release` (run.app answers again).
- SPA: point the FE rewrites back at run.app (`vercel.json`) and unset `VITE_API_ORIGIN`.
- Edge: remove the Worker Custom Domain; the API is still reachable on run.app while `off`.

## Leftover from the ADR-0022 run

The owner checks `gcloud compute forwarding-rules list --project=aimos-prod`. If the static IP
`136.82.167.148` has no forwarding rule, release it (about $7.30 a month unattached). The NEGs and
backend services can stay; `edge setup` recreates what is missing later.

## DPP answer draft (only after L1-L3 and L7, ADR-0025 interim gate)

"Yes. Internet traffic to our API passes through Cloudflare's edge: a managed web application
firewall ruleset, always-on L3-L7 DDoS mitigation, method and path allow-lists and per-IP rate
limiting, with TLS 1.2 or higher. The origin rejects any API request that did not come through that
edge (health probes excepted). Network segmentation: the database has no public access path (no
authorised networks; only our services reach it through an IAM-authenticated, encrypted
connector); each workload runs under its own least-privilege identity; staging and production are
separate cloud projects. Intrusion prevention is the WAF and DDoS layer, with security events
logged. Staff devices run OS-native anti-malware (Microsoft Defender / macOS XProtect), kept up to
date." Do not claim OWASP CRS, IDS sensors or upload malware scanning; the laptop line is true
only once the client confirms it.

Evidence kept: the L3 Security Events screenshot, the L7 account screenshots, `edge status` output.
