# External setup for v1: marketplaces, cloud, domain and keys

The backend is built and tested end to end against local fakes: a fake Amazon/Flipkart OAuth server, a FakeAdapter, MinIO, a local KMS stub and a hash embedder. Going live needs the items below. Only the client can do them, and several take weeks, so start the long ones first.

Status tracking: TEAM-QUESTIONS T77 (marketplace applications), T78 (Flipkart sandbox), STATE "Pending user decisions" (billing, domain).

## 0. Do first: the domain and a public website (both marketplaces check them)

1. **Buy the apex domain and serve HTTPS.**
   - The API callback host is planned as `https://api.<domain>`; the dashboard as `https://app.<domain>`.
   - Both marketplaces refuse non-https redirect URIs, so the domain blocks both applications.
2. **Publish a public business website on it.** Amazon rejects sites that are:
   - under construction;
   - login-only;
   - inaccessible;
   - showing a security warning.

   The site needs:
   - a clear description of what the product does for marketplace sellers;
   - a **privacy policy**;
   - **terms of service**;
   - a contact address.
3. Keep the company documents at hand: incorporation, GST and the registered address.

## 1. Amazon (Selling Partner API, amazon.in)

We connect *other brands'* seller accounts, so we register as a **public developer**. A private app only accesses its own account.

### 1.1 Register the developer profile
1. Sign in to the **Solution Provider Portal**, or to Seller Central at *Apps and Services → Develop Apps*, with the client's own seller or developer account.
2. Complete the **developer profile**:
   - **Contact information:** organization, website (from step 0), country, primary contact.
   - **Data access:** choose *Public Developer: I build and offer publicly available applications that are used by other sellers*.
   - **Roles:** request only what v1 uses (§1.2).
   - **Use cases:** describe the product plainly. It generates marketplace-compliant listings from the brand's product data; a human approves every listing before it is published; the app writes listings and reads listing status and sales performance.
   - **Security controls:** answer from our real design (§1.4).
   - Accept the **Acceptable Use Policy** and the **Data Protection Policy**.
3. Submit, then **answer every Amazon follow-up within 5 days**. Slow replies are the most common cause of a stalled review.

### 1.2 Roles to request (no restricted roles: this keeps the review short)
- **Product Listing:** create and update listings, read product type definitions and the catalog. S20 and S21 use it.
- **Pricing:** prices on listings.
- **Inventory and Order Tracking:** stock and order reports for the analytics metrics (S23).
- **Do not request** any role that exposes buyer PII (Direct-to-Consumer Shipping, Tax Invoicing, and similar). v1 never needs buyer names or addresses. Restricted roles add a business verification, a PII security assessment and an architecture review, which cost weeks.

### 1.3 After the profile is approved: register the app
1. Create the app in the portal. The backend needs three values:

   | Value | Setting |
   |---|---|
   | App ID | `AMAZON_APP_ID` |
   | LWA client ID | `AMAZON_CLIENT_ID` |
   | LWA client secret | `AMAZON_CLIENT_SECRET` |

   Put the secret in **Secret Manager**, never in a file or a chat.
2. Set the **OAuth Redirect URI** to the exact string in ENV.md, one per environment:
   - `https://api.<domain>/api/v1/marketplaces/amazon/callback`
   - the staging equivalent

   It must match byte for byte.
3. Set the **OAuth Login URI** to the dashboard's sign-in page, `https://app.<domain>/login`. v1 starts the connection from our dashboard (the website flow), so Amazon goes straight to the Redirect URI. The Login URI is used only if a seller later starts from the Appstore.
4. **Test with real sellers before the app is published.** A draft app can only be authorized with `version=beta` on the consent URL. Set:

   `AMAZON_AUTHORIZE_URL=https://sellercentral.amazon.in/apps/authorize/consent?version=beta`

   The backend keeps that query and adds `application_id`, `redirect_uri` and `state`. Remove `?version=beta` once the app is published.
5. Connect one or two friendly brands (the pilot brand) with that URL to prove the real flow end to end. The plan calls this DW9.
6. Keep `AMAZON_USE_SANDBOX=true` until production. The backend then pins the SP-API host to Amazon's sandbox EU host.

### 1.4 Security answers we can give truthfully
- **Tokens:**
  - refresh and access tokens are envelope-encrypted with Cloud KMS;
  - they are decrypted only inside the adapter or worker that calls Amazon;
  - they never reach a prompt or a log, and Sentry and the logs redact them;
  - revoke shreds the stored tokens.
- **Tenancy:** every query is scoped by tenant in the repository layer **and** by PostgreSQL row-level security.
- **Data:** no buyer PII is requested or stored; data is in GCP `asia-south1` (India); access needs auth with RBAC, and audit logs are append-only.
- **Engineering:** CI scans for secrets (gitleaks) and dependency vulnerabilities (pip-audit) on every change.

## 2. Flipkart (Marketplace Seller APIs)

### 2.1 Register as an API partner (multi-seller)
1. Register on the **Flipkart Partner Dashboard** and answer **"Yes"** to "Are you an API Partner?".
2. Flipkart verifies the details, usually within about 72 hours.
3. Then open *Profile → Manage API Access → Create Application* and choose a **third-party application**.
   - **The app type matters.** Aggregators and partners *must* use a third-party app; a self-access app used for other sellers risks a permanent ban.
4. Give the app:
   - a name and a description;
   - the **https** redirect URL `https://api.<domain>/api/v1/marketplaces/flipkart/callback` (it must match exactly).
5. Flipkart issues an **Application ID** and an **Application Secret**. They go into `FLIPKART_CLIENT_ID` and `FLIPKART_CLIENT_SECRET`; the secret goes into Secret Manager.

### 2.2 How the connect works (already built in S15)
- Authorization code flow:
  - `https://api.flipkart.net/oauth-service/oauth/authorize?client_id=…&redirect_uri=…&response_type=code&scope=Seller_Api&state=…`
  - then the token exchange at `https://api.flipkart.net/oauth-service/oauth/token`, with Basic auth (app id and secret).
- Set `FLIPKART_AUTHORIZE_URL` to the authorize URL above, without the query; the backend adds it.

### 2.3 Ask Flipkart, when you register
- **Is there a sandbox or test seller account for partners?** We have not confirmed one (T78); Flipkart has published a sandbox host for its API documentation. If there is none, the first real publish is one supervised listing on a live test SKU.
- **Which API areas the app may call:** listing management, inventory and price, order and report data (for analytics).

## 3. Cloud and keys (the backend's runtime)

| Item | Needed for | Who |
|---|---|---|
| **GCP billing account**, on the two projects `aimos-staging` and `aimos-prod` | staging and prod: Cloud Run, Cloud SQL, GCS, Secret Manager, Cloud KMS | client |
| **Cloud KMS** key ring `marketplace-os`, key `oauth-token-key` | sealing marketplace tokens | applied by OpenTofu at S4 once billing is on |
| **OpenAI API key** | brand-voice embeddings (S16) and the AI team's generation | client, into Secret Manager |
| **Sentry DSN** (staging, prod) | error reporting; deployed boots refuse to start without one | client |
| **GitHub token with `read:packages`** for the FE and AI teams | installing `@mkos/schemas` and `mkos-schemas` | repo admin |

## 4. Order of work (the critical path)

1. **Week 0:** domain plus website (§0) → **file the Amazon developer profile** (§1.1) → **register on the Flipkart Partner Dashboard** (§2.1) → turn on GCP billing.
2. **When approved:** create both apps, set the redirect URIs, and store the secrets.
3. **Then:** deploy staging (S4) → connect the pilot brand on Amazon with `version=beta` and on Flipkart → a supervised publish of one listing per marketplace → production.
