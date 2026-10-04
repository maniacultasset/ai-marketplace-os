# Environment register

The single consolidated list. Local values match `docker-compose.yml` exactly so the bootstrap works on a clean machine. Production values come from Secret Manager, never from a file in git.


## App

| Variable | Local value | Source | Required | Notes |
|---|---|---|---|---|
| `APP_ENV` | `local` | local .env | required | local \| staging \| production. THE single environment switch: admin kill-switch, Sentry environment tag and LocalKmsStub selection all read this. `ENV` is forbidden. |
| `APP_ROLE` | `api` | local .env | optional | api \| worker \| migrate \| shell; default api (ADR-0013 §1). Picks the boot requirements: api needs DATABASE_URL and JWT_SECRET, worker needs DATABASE_URL, migrate needs ALEMBIC_DATABASE_URL, shell nothing more. The image sets it per role. |
| `APP_NAME` | `marketplace-os-api` | local .env | optional | Worker deployment overrides to marketplace-os-worker. |
| `API_V1_PREFIX` | `/api/v1` | local .env | required | Never change. /healthz, /readyz, /openapi.json, /docs, /internal/admin sit outside it. |
| `LOG_LEVEL` | `INFO` | local .env | optional | DEBUG locally if you want SQL echo context; INFO everywhere else. |
| `LOG_FORMAT` | `console` | local .env | optional | console locally for readability; json in staging/prod (Cloud Run stdout is the transport). |
| `ALLOWED_ORIGINS` | `http://localhost:3000` | local .env | required | Comma-separated. staging: https://app-staging.<domain>; prod: https://app.<domain>. |
| `REQUEST_MAX_BODY_BYTES` | `10485760` | local .env | required | 10 MiB hard cap. Cloud Run rejects bodies over 32 MiB; the 50 MB multipart bulk path is forbidden — large CSVs go via signed GCS upload URL. |
| `RATE_LIMIT_PER_TENANT_PER_MIN` | `120` | local .env | required | The 600 figure is deleted. |
| `RATE_LIMIT_PER_IP_PER_MIN` | `60` | local .env | required | Applies to unauthenticated routes. |
| `AUTH_LOGIN_RATE_LIMIT_PER_MIN` | `5` | local .env | required | Per IP + per email on POST /api/v1/auth/login. |
| `OAUTH_STATE_TTL_SECONDS` | `600` | local .env | required | Marketplace OAuth state parameter lifetime (Redis-backed). |
| `ADMIN_ENABLED` | `true` | local .env | required | The internal SQLAdmin mounts only when ADMIN_ENABLED=true AND APP_ENV != production. CI test asserts /internal/admin returns 404 with APP_ENV=production. |
| `ADMIN_BASE_URL` | `/internal/admin` | local .env | optional | Unversioned by design. |
| `ADMIN_READ_ONLY` | `true` | local .env | required | false only on api-staging, to allow DLQ requeue. |
| `ADMIN_SESSION_SECRET` | `dev_only_admin_session_secret_at_least_32_chars` | both | required | Secret Manager in staging/prod; 32+ random bytes. |
| `ADMIN_STAFF_EMAILS` | `(empty — leave blank)` | local .env | optional | Comma-separated allowlist, second layer behind IAP/IAM. |
| `ADMIN_LOCAL_EMAIL` | `dev@local` | local .env | optional | Used only when APP_ENV=local. |
| `PORT` | `8000` | local .env | required | 8000 locally. Cloud Run injects `$PORT` (image default 8080); the worker health server binds `settings.port`, which is why this is a register row and not a hard-coded constant. |

## Database

| Variable | Local value | Source | Required | Notes |
|---|---|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/marketplace_os` | both | required | Matches docker-compose exactly (POSTGRES_USER/PASSWORD/DB = postgres/postgres/marketplace_os). postgres_dev_password and password are forbidden. Secret Manager in staging/prod, connecting as app_rw. |
| `ALEMBIC_DATABASE_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os` | both | required | Sync driver for Alembic. In staging/prod this is the `migrator` role, used only by the pre-deploy Cloud Run migration job. |
| `DATABASE_POOL_SIZE` | `5` | local .env | required | 5, not 10 — connection budget is (api max-instances + worker max-instances) × 10; the arithmetic lives in the Cloud Run ADR. |
| `DATABASE_MAX_OVERFLOW` | `5` | local .env | required | Engine also sets pool_pre_ping=True, pool_recycle=1800 (Cloud SQL drops idle connections). |
| `DATABASE_ECHO` | `false` | local .env | optional | Never true outside local. |
| `DATABASE_POOL_TIMEOUT` | `10` | local .env | required | Seconds to wait for a pooled connection before raising. |
| `DATABASE_POOL_RECYCLE` | `1800` | local .env | required | Cloud SQL drops idle connections; recycle below that window. |
| `DB_STATEMENT_TIMEOUT_MS` | `15000` | local .env | required | Server-side `statement_timeout` for every app connection, passed as the libpq `options` connect argument (psycopg 3; the `server_settings` form is asyncpg-only). |

## GCS

| Variable | Local value | Source | Required | Notes |
|---|---|---|---|---|
| `STORAGE_BACKEND` | `minio` | local .env | required | minio locally, gcs in staging/prod. |
| `GCS_BUCKET_IMAGES` | `marketplace-images` | local .env | required | Exactly the bucket the compose `mc mb` one-shot creates. staging: aimos-images-staging; prod: aimos-images-prod. |
| `GCS_BUCKET_CSV` | `marketplace-csv` | local .env | required | Exactly the bucket the compose `mc mb` one-shot creates. staging: aimos-csv-staging; prod: aimos-csv-prod. |
| `S3_ENDPOINT_URL` | `http://localhost:9000` | local .env | optional | MinIO only. Unset in staging/prod. |
| `S3_ACCESS_KEY` | `local_only_minio_root_user` | local .env | optional | Interpolated as MINIO_ROOT_USER (`:?` form; a missing `.env` fails). |
| `S3_SECRET_KEY` | `local_only_minio_root_password_32_chars` | local .env | optional | Interpolated as MINIO_ROOT_PASSWORD (`:?` form; a missing `.env` fails). minioadmin_dev_password is forbidden. |

## GCP

| Variable | Local value | Source | Required | Notes |
|---|---|---|---|---|
| `GCP_PROJECT_ID` | `aimos-dev` | local .env | required | GOOGLE_CLOUD_PROJECT and GCP_PROJECT are forbidden. Projects: aimos-dev, aimos-staging, aimos-prod. |
| `GCP_REGION` | `asia-south1` | local .env | required | Mumbai (D9). Cloud SQL cannot be moved later. |
| `KMS_KEY_URI` | `(empty — leave blank locally)` | local .env | optional | Empty locally: APP_ENV=local selects LocalKmsStub. staging/prod: projects/<project>/locations/asia-south1/keyRings/marketplace-os/cryptoKeys/oauth-token-key. KMS_KEY_NAME is forbidden. Read through Settings, never os.environ, and construct the KMS client lazily inside the function. |
| `KMS_LOCAL_DEV_KEY` | `bG9jYWxfZGV2X29ubHlfMzJfYnl0ZV9rZXlfMDAwMDA=` | local .env | optional | base64 32 bytes; LocalKmsStub only. Never set in staging/prod. |
| `SECRET_MANAGER_ENABLED` | `false` | local .env | required | true in staging/production, where secrets arrive via `gcloud run deploy --set-secrets`. |
| `GIT_SHA` | `dev` | local .env | optional | Injected by Cloud Build; used as the Sentry release tag. |

## JWT

| Variable | Local value | Source | Required | Notes |
|---|---|---|---|---|
| `JWT_SECRET` | `dev_only_change_me_at_least_32_characters_long` | both | required | HS256. MUST differ per environment or a staging token works in production. Secret Manager in staging/prod. |
| `JWT_ALGORITHM` | `HS256` | local .env | required | PyJWT[crypto], never python-jose. |
| `JWT_ISSUER` | `http://localhost:8000` | local .env | required | staging: https://api-staging.<domain>. |
| `JWT_AUDIENCE` | `ai-marketplace-os` | local .env | required | Claims frozen as {sub, tid, roles[], email, typ, jti, iss, aud, iat, exp}. |
| `ACCESS_TOKEN_TTL_MINUTES` | `30` | local .env | required | FastAPI is the SOLE issuer (D6); NextAuth holds our tokens as an opaque credential. |
| `REFRESH_TOKEN_TTL_DAYS` | `14` | local .env | required | Rotation with reuse detection; refresh_tokens in Postgres is the source of truth for logout. |
| `PASSWORD_HASH_SCHEME` | `argon2id` | local .env | required | pwdlib[argon2], never passlib. |

## LLM

| Variable | Local value | Source | Required | Notes |
|---|---|---|---|---|
| `LLM_PROVIDER` | `openai` | local .env | required | OpenAI only for MVP; ModelRouter reads a per-provider key map. |
| `OPENAI_API_KEY` | `sk-REPLACE_ME` | both | required | LLM_API_KEY is forbidden. Client-supplied, blocking by Day 10. Secret Manager in staging/prod; the AI team's GCP group holds secretAccessor on this and on nothing else. |
| `DEEPSEEK_API_KEY` | `(empty — leave blank)` | local .env | optional | Deferred; do not open a DeepSeek account for MVP. |
| `LLM_MODEL_DEFAULT` | `gpt-4o-mini` | local .env | required | Text + vision default. |
| `LLM_MODEL_ESCALATION` | `gpt-4o` | local .env | required | Tier-2 on a second JSON failure. |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | local .env | required | Pinned in an ADR; brand_chunks.embedding_model records it per row so a mixed-model table is detectable. |
| `EMBEDDING_DIMENSIONS` | `1536` | local .env | required | Hard DB constraint — vector(1536). Changing it is a table rewrite and a re-embed. |
| `LLM_TIMEOUT_SECONDS` | `60` | local .env | required | Per model call, well under the 300s generation job timeout. |
| `LLM_MAX_CORRECTION_ATTEMPTS` | `3` | local .env | required | Enforced INSIDE the graph edge logic (invariant 9); ARQ max_tries=1 for generation so retries cannot multiply it. |
| `LLM_DAILY_BUDGET_USD` | `12.00` | local .env | required | USD, matching agent_runs.cost_usd. The *_INR variants are deleted — no fx table exists. |
| `LLM_TENANT_DAILY_BUDGET_USD` | `2.50` | local .env | required | Kill switch computed from SUM(cost_usd) per tenant per UTC day. |

## Amazon

| Variable | Local value | Source | Required | Notes |
|---|---|---|---|---|
| `AMAZON_APP_ID` | `amzn1.sp.solution.REPLACE_ME` | both | required | From the SP-API developer application (2–4 week approval; application filed Day 2). |
| `AMAZON_CLIENT_ID` | `amzn1.application-oa2-client.REPLACE_ME` | both | required | LWA client id. |
| `AMAZON_CLIENT_SECRET` | `REPLACE_ME` | Secret Manager | required | Never in a committed .env. Sentry/log redaction deny-list includes it. |
| `AMAZON_LWA_TOKEN_URL` | `https://api.amazon.com/auth/o2/token` | local .env | required | Same in all environments. |
| `AMAZON_SPAPI_ENDPOINT` | `https://sellingpartnerapi-eu.amazon.com` | local .env | required | India sits in the EU region group. AMAZON_SP_API_ENDPOINT (with underscore split) is forbidden. |
| `AMAZON_MARKETPLACE_ID` | `A21TJRUUN4KGV` | local .env | required | amazon.in. |
| `AMAZON_OAUTH_REDIRECT_URI` | `http://localhost:8000/api/v1/marketplaces/amazon/callback` | local .env | required | THIS EXACT STRING goes into the Amazon developer application, along with the staging and prod forms. Changing it later means re-review. AMAZON_LWA_REDIRECT_URI is forbidden. |
| `AMAZON_USE_SANDBOX` | `true` | local .env | required | false only in prod, after the application clears. |

## Flipkart

| Variable | Local value | Source | Required | Notes |
|---|---|---|---|---|
| `FLIPKART_CLIENT_ID` | `REPLACE_ME` | both | required | From the Flipkart Seller API application. |
| `FLIPKART_CLIENT_SECRET` | `REPLACE_ME` | Secret Manager | required | Never in a committed .env; in the redaction deny-list. |
| `FLIPKART_TOKEN_URL` | `https://api.flipkart.net/oauth-service/oauth/token` | local .env | required | Same in all environments. |
| `FLIPKART_API_BASE` | `https://api.flipkart.net/sellers` | local .env | required | India-only API. |
| `FLIPKART_OAUTH_REDIRECT_URI` | `http://localhost:8000/api/v1/marketplaces/flipkart/callback` | local .env | required | Exact string for the application; FLIPKART_REDIRECT_URI is forbidden. |

## Observability

| Variable | Local value | Source | Required | Notes |
|---|---|---|---|---|
| `SENTRY_DSN` | `(empty — leave blank)` | both | optional | Empty locally disables Sentry. Secret Manager in staging/prod. send_default_pii=False, plus a before_send scrubber. |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.1` | local .env | optional | 0.1 everywhere. The 1.0 value in the admin draft is deleted. Environment tag comes from APP_ENV, release from GIT_SHA — SENTRY_ENVIRONMENT is forbidden. |
| `OTEL_SERVICE_NAME` | `marketplace-os-api` | local .env | required | Worker deployment sets marketplace-os-worker. Bare `api`/`worker` are forbidden. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `(empty — leave blank)` | local .env | optional | Empty by design in every environment: GCP exports direct to Cloud Trace via opentelemetry-exporter-gcp-trace. Set http://localhost:4317 only if you personally run Jaeger — compose ships no collector. |
| `OTEL_TRACES_SAMPLER` | `parentbased_traceidratio` | local .env | optional | Standard OTel var name. |
| `OTEL_TRACES_SAMPLER_ARG` | `0.1` | local .env | optional | 0.1 everywhere; /healthz and /readyz are excluded from tracing and access logs. |
| `ALERT_SLACK_WEBHOOK` | `(empty — leave blank)` | both | optional | Secret Manager in staging/prod; queue-latency and budget alerts post here. |
