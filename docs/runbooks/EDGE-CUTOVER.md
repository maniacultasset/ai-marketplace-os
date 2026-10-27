# API edge cutover: Cloud Armor on a global external load balancer (prod)

This runbook puts `api.kuinn.in` in front of the prod API: a global external Application Load
Balancer with a Google-managed certificate and an SSL policy MODERN / minimum TLS 1.2, Cloud Armor
Standard (`kuinn-api-waf`, rules in `scripts/deploy/gcp/armor-rules.yaml`), then closes the
Cloud Run `run.app` and tag URLs with ingress `internal-and-cloud-load-balancing`. The decision
and its accepted risks are ADR-0022. Every command is `scripts/deploy/gcp/deploy.sh prod ...`
(`docs/runbooks/DEPLOY-GCP.md`); each prints its gcloud calls, and `--dry-run` runs none.

Nothing here starts before `kuinn.in` resolves (step 0). Staging has no edge (`EDGE_DOMAIN` is
blank there, and `staging edge` is refused).

## Architecture

```
browser (SPA on Vercel, VITE_API_ORIGIN=https://api.kuinn.in)
   |  HTTPS, TLS >= 1.2 (SSL policy aimos-api-edge-ssl, MODERN)
   v
aimos-api-edge-ip  ->  forwarding rule aimos-api-edge-https (:443 only, no :80)
   -> target HTTPS proxy aimos-api-edge-proxy (cert aimos-api-edge-cert, Google-managed)
   -> URL map aimos-api-edge-map
        header x-aimos-candidate: 1 -> backend aimos-api-edge-candidate-be -> NEG aimos-api-edge-candidate-neg
                                        (Cloud Run tag `candidate`: the revision a deploy is smoking)
        everything else             -> backend aimos-api-edge-be           -> NEG aimos-api-edge-neg
                                        (Cloud Run service aimos-api, asia-south1)
   both backends: EXTERNAL_MANAGED, request logging 100 %, Cloud Armor policy kuinn-api-waf
Cloud Run aimos-api: ingress internal-and-cloud-load-balancing after step 9 (run.app refused)
```

## Endpoint inventory (every internet-facing endpoint and its control)

| Endpoint | Control |
|---|---|
| API `https://api.kuinn.in` | Cloud Armor (path and method allow-lists, OWASP CRS 4.22, per-IP rate limits, Layer 7 DDoS defense), TLS >= 1.2, the app's own auth and rate limits |
| Cloud Run `run.app` service and tag URLs | refused after `edge lock-ingress` (step 9) |
| SPA on Vercel | the Vercel platform firewall (needs Vercel Pro) |
| GCS signed-URL PUT/GET on `storage.googleapis.com` | short-lived V4 signed URLs, bucket CORS for the SPA origins (`edge cors`), no WAF |
| Cloud SQL | public IP with no authorised networks, reached only through the connector |

## Honest wording (DPP 1.1.1, 1.1.3, 1.5)

Answer DPP 1.1.1 "yes" only after `path`, `rate` and every CRS group, sqli and xss included, are
enforced and `edge status` prints `preview rules left: 0`. Until then the answer is "WAF
deployed, in tuning (preview)". The claim covers the API; the SPA counts only behind the Vercel
platform firewall on Pro. State the inspection limit: the preconfigured rules read the first
64 kB of a body while the app accepts up to 10 MiB, and the upload routes and the auth paths whose
body carries a password, a code or a refresh token skip CRS; app validation covers both. The
IDS/IPS line is "Cloud Armor CRS plus Adaptive Protection (Standard, basic alerts to email)";
Amazon judges whether that is equivalent. DPP 1.5: the SSL policy refuses TLS 1.0 and 1.1 at the
load balancer.

## Steps

0. Prereqs, read from live state, not from the plan:
   - `kuinn.in` DNS is managed; the `api` name will carry only the load balancer's A record; any
     CAA record on the apex allows `pki.goog` or `letsencrypt.org`; Vercel Pro on the account
     that serves prod; ADR-0022 accepted; the client is ready to edit the marketplace portals.
   - Go-live sequencing: read `ALLOWED_ORIGINS` and `DASHBOARD_BASE_URL` off the running service
     (`gcloud run services describe aimos-api --region=asia-south1 --project=aimos-prod`), and
     list every Vercel project and domain that serves the prod SPA (the old
     `ai-marketplace-os-web.vercel.app` project, and the `kuinn.in` project if the go-live has
     run). Every frontend step below applies to each such project. A project that no longer serves
     prod is removed and its origin dropped from `ALLOWED_ORIGINS` before step 9. `prod.env` must
     match the live values before step 8.
   - Prod-deploy lanes: every checkout that can run `prod release` (`WS/mkos-lanes/gcp`, the
     prod deploy lane, and any hotfix or unrebased feature lane) is rebased onto the DPP-2 commit
     or retired. Older checkouts hard-code `--ingress=all` and would silently reopen run.app
     around the WAF; the live-state guard in `deploy` exists only from DPP-2 on.

   Rollback: nothing has changed yet.
1. `prod setup` (it now enables `compute.googleapis.com` and `monitoring.googleapis.com`). Then
   delete the auto-created `default` VPC network if it exists and nothing uses it.

   Rollback: the APIs can stay enabled; they cost nothing unused.
2. `prod edge setup`, then the A record for `api.kuinn.in` at the printed IP, then wait until
   `prod edge status` shows the certificate ACTIVE **and** `edge serving: yes` (a real 401 from
   `https://api.kuinn.in/api/v1/auth/me` with a valid certificate; the load balancer can take
   about 30 more minutes after ACTIVE). That gate must still pass at the start of step 8.

   Rollback: remove the DNS record, then delete the edge resources in reverse order (forwarding
   rule, proxy, certificate, SSL policy, URL map, backends, NEGs, policy, IP).
3. `prod edge rules` (every rule in preview), then set `ALERT_EMAIL` in `prod.env` and run
   `prod edge alerts`.

   Rollback: `EDGE_ENFORCE=` and `prod edge rules`; preview rules never block.
4. Measure the forwarded-for hops on the direct browser path: from a known IP,
   `curl https://api.kuinn.in/api/v1/auth/me` once with no `X-Forwarded-For` and once with a
   forged one. Read `xff_entries` on the matching access lines in Cloud Logging. The
   `HOPS["production"]` value is the count without the forged entry; the forged request must show
   that count plus one. The Vercel-rewrite path is measured in step 8. The check that tag URLs
   follow ingress is not here (ingress is still `all`); `edge lock-ingress` runs it (step 9).

   Rollback: nothing changed.
5. Open the HOPS PR (the one `HOPS` row in `apps/api/app/api/rate_limit.py` plus its
   `test_client_ip` cases): **open and approved, not merged.** `main` is the release branch, so a
   merged HOPS change would ship with any hotfix while run.app is still open, and a direct caller
   could then pick its rate-limit bucket with a forged header. It is merged inside the step-8
   window, right before the release.

   Rollback: close the PR.
6. Read the live issuer
   (`gcloud run services describe aimos-api --region=asia-south1 --project=aimos-prod --format=json`,
   the `JWT_ISSUER` env value) and write it to `prod.env` as `JWT_ISSUER=`. Refresh tokens are
   checked against the issuer, so this keeps every user signed in when `API_PUBLIC_URL` changes.
   The live-state guard refuses any later deploy that would change it.

   Rollback: blank the pin (only before step 8; after it, a change is refused and signs users out).
7. Portal redirect URIs at Amazon (and Flipkart when it is enabled): add
   `https://api.kuinn.in/api/v1/marketplaces/amazon/callback` (and `/flipkart/callback`), and keep
   the run.app one where the portal takes several. If the go-live has not registered the apps yet,
   register the `api.kuinn.in` URIs from the start so the client edits each portal once.

   Rollback: remove the added URI.
8. The cutover window, in this order:
   1. Re-check `edge serving: yes` with `prod edge status`. Merge the HOPS PR.
   2. Set `API_PUBLIC_URL=https://api.kuinn.in` in `prod.env`, then `prod release` (ships the
      HOPS change and moves the redirect URIs; the pinned `JWT_ISSUER` stays).
   3. Frontend, per serving Vercel project, in three ordered deploys: (a) the CSP PR (adds
      `https://api.kuinn.in` and the two bucket paths to `connect-src`, keeps run.app; harmless
      while the bundle is same-origin, and it can ship days early); (b) the rewrites pointed at
      `api.kuinn.in`, with a host condition on the production domain, in the same or the next
      deploy; (c) only after (a) is live, set `VITE_API_ORIGIN=https://api.kuinn.in` for the
      **Production** scope only and redeploy, then check that the live CSP header lists
      `https://api.kuinn.in`. Setting the env first and rebuilding an old commit blocks every API
      call by CSP.
   4. The end-to-end checks: login, refresh, OAuth connect, CSV import, image upload (GCS PUT),
      the `/readyz` network banner, no CSP violations. Also read `xff_entries` for a call through
      the Vercel rewrite to `api.kuinn.in`. If it differs from the step-4 count, rewritten users
      share one bucket (the Vercel egress IP) during the grace period: shorten the step-10 grace
      and note it under ADR-0022's accepted risks.

   Rollback: revert `API_PUBLIC_URL` and `prod release`, revert the frontend PRs, revert the HOPS
   PR. The issuer is pinned, so nobody is signed out.
9. Lock, in the **same window**, right after the step-8 checks: set
   `INGRESS=internal-and-cloud-load-balancing` in `prod.env`, then `prod edge lock-ingress`. It
   refuses unless the load balancer smoke passes and every SPA origin's bundle and CSP name
   `https://api.kuinn.in` and its `/readyz` works with that origin's CORS grant. It removes stale
   `sha-*` tags, locks, probes run.app and every remaining tag URL (`candidate`), rechecks
   `/readyz`, and unlocks again by itself if anything still answers directly or the SPA breaks. Do
   not end the window unlocked; if the lock cannot be done, revert the HOPS PR and redeploy before
   ending it. From now on `prod deploy` smokes a new revision through the load balancer with the
   header `x-aimos-candidate: 1`, and refuses to run with `INGRESS=all` while the live service is
   locked.

   Rollback: `prod edge unlock-ingress`, and `INGRESS=all` in `prod.env`.
10. 48 hours later, remove the frontend rewrites. Until then every Vercel deployment can reach the
    prod API same-origin through a rewrite without a host condition, so "previews blocked by CORS"
    holds only after this step (or sooner with the host condition).

    Rollback: restore the rewrites (pointed at `api.kuinn.in`, never at run.app).
11. Preview for 7 to 14 days. Tune false positives by adding header exclusions or
    `opt_out_rule_ids` to `armor-rules.yaml` and rerunning `prod edge rules`. Deliberately upload a
    real CSV, an image through the fallback route and a PDF/DOCX brand document, and check the
    preview hits (`jsonPayload.previewSecurityPolicy` in the load balancer logs).

    Rollback: revert the `armor-rules.yaml` change and rerun `prod edge rules`.
12. Enforce through `EDGE_ENFORCE` (quoted, space-separated) and `prod edge enforce`: `path`, then
    `rate`, then one CRS group a day in priority order (lfi, rfi, rce, protocolattack,
    scannerdetection, then sqli, xss). Skipping a noisy tier-2 set is allowed, but then requests
    that set shadows in the preview logs are not seen for later sets: query
    `previewSecurityPolicy` by `preconfiguredExprIds` before each later enforce. After each
    enforce, run `EDGE_DENY_ALERT=10 prod edge alerts` and rerun it plain 48 hours later. Armor's
    own 403/404/429 carry no `Access-Control-Allow-Origin` (the app adds CORS), so a browser sees
    them as a network error: treat a rise in "can't reach the server" reports as a likely false
    positive and read the deny logs first. Once, after `path` is enforced, check that the frontend
    shows a forced deny as "blocked or limited", not offline.

    Rollback: drop the last group from `EDGE_ENFORCE` and run `prod edge rules`.
13. Evidence for DPP 1.1.3: the `prod edge status` output (the policy export, with
    `preview rules left: 0`), the architecture diagram above, the endpoint inventory, deny samples
    from the `security-logs` bucket, and the alert policy list.

    Rollback: nothing to undo.
14. Close the KNOWN-ISSUES.md hop-count row for prod with the measured value.

    Rollback: reopen it if a later measurement differs.

## Rollbacks in short

- Any rollback after step 9 starts with `prod edge unlock-ingress` and `INGRESS=all` in
  `prod.env`, then the rest. Reverting the frontend PR or a Vercel Instant Rollback while ingress
  is locked points the SPA back at a refused run.app (a full outage), and a redeploy that moves
  `API_PUBLIC_URL` off the edge is refused while `INGRESS` is locked.
- Edge resources: remove them in reverse order; DNS: remove the record.
- Rules: `EDGE_ENFORCE=` and `prod edge rules`.
- HOPS: revert the PR.
- Issuer: blank the pin, only before step 8.
- Cutover (after the unlock, or before step 9): revert `API_PUBLIC_URL` and redeploy, and revert
  the frontend PR.
- Lock: `prod edge unlock-ingress` and `INGRESS=all`.

## Cost

About $40 a month (list prices): the forwarding rule, the policy and its rules, Cloud Armor request
charges, load balancer data processing, and the 400-day `security-logs` bucket.
