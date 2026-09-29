"""THE env register. Nothing else in the codebase reads os.environ.

A field without a default is REQUIRED and a missing one aborts the process at
import. There are exactly four, which is why the CI env block has exactly four.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8",
                                      extra="ignore", case_sensitive=False)

    # ---- REQUIRED everywhere ----
    app_env: Literal["local", "staging", "production"]
    database_url: str = Field(pattern=r"^postgresql\+asyncpg://")
    redis_url: str = Field(pattern=r"^rediss?://")
    jwt_secret: str = Field(min_length=32)

    # ---- app ----
    app_name: str = "marketplace-os-api"; api_v1_prefix: str = "/api/v1"; git_sha: str = "dev"
    port: int = 8080
    allowed_origins: str = "http://localhost:3000"        # CSV, read via .cors_origins
    log_level: str = "INFO"; log_format: Literal["json", "console"] = "json"
    request_max_body_bytes: int = 10_485_760              # Cloud Run hard cap is 32 MiB
    rate_limit_per_tenant_per_min: int = 120; rate_limit_per_ip_per_min: int = 60
    auth_login_rate_limit_per_min: int = 5

    # ---- database (pool is PER PROCESS; see ADR-0003 arithmetic) ----
    alembic_database_url: str | None = None
    database_pool_size: int = 5; database_max_overflow: int = 5
    database_pool_timeout: int = 10; database_pool_recycle: int = 1800
    database_echo: bool = False; db_statement_timeout_ms: int = 15_000

    # ---- queue: one queue per worker deployment ----
    arq_queue_name: str = "q:default"; arq_max_jobs: int = 10
    arq_job_timeout_seconds: int = 300

    # ---- storage ----
    storage_backend: Literal["minio", "gcs"] = "minio"
    gcs_bucket_images: str = "marketplace-images"; gcs_bucket_csv: str = "marketplace-csv"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"; s3_secret_key: str = "minioadmin"

    # ---- gcp ----
    gcp_project_id: str | None = None; gcp_region: str = "asia-south1"
    kms_key_uri: str | None = None   # .../keyRings/marketplace-os/cryptoKeys/oauth-token-key
    kms_local_dev_key: str = "bG9jYWwtZGV2LWtleS0zMi1ieXRlcy1leGFjdGx5ISE="  # LocalKmsStub
    secret_manager_enabled: bool = False

    # ---- auth ----
    jwt_algorithm: Literal["HS256"] = "HS256"
    jwt_issuer: str = "http://localhost:8000"; jwt_audience: str = "ai-marketplace-os"
    access_token_ttl_minutes: int = 30; refresh_token_ttl_days: int = 14

    # ---- models: per-provider keys (one LLM_API_KEY cannot serve two providers) ----
    llm_provider: Literal["openai", "deepseek"] = "openai"
    openai_api_key: str | None = None; deepseek_api_key: str | None = None
    llm_model_default: str = "gpt-4o-mini"; llm_model_escalation: str = "gpt-4o"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: Literal[1536] = 1536            # pinned: the HNSW column type
    llm_timeout_seconds: int = 60; llm_max_correction_attempts: int = 3
    llm_daily_budget_usd: float = 12.0; llm_tenant_daily_budget_usd: float = 2.5

    # ---- marketplaces: these redirect URIs are the strings filed in the apps ----
    amazon_client_id: str | None = None; amazon_client_secret: str | None = None
    amazon_oauth_redirect_uri: str = "http://localhost:8000/api/v1/marketplaces/amazon/callback"
    amazon_spapi_endpoint: str = "https://sellingpartnerapi-eu.amazon.com"
    amazon_marketplace_id: str = "A21TJRUUN4KGV"; amazon_use_sandbox: bool = True
    flipkart_client_id: str | None = None; flipkart_client_secret: str | None = None
    flipkart_oauth_redirect_uri: str = "http://localhost:8000/api/v1/marketplaces/flipkart/callback"

    # ---- observability + internal admin ----
    sentry_dsn: str | None = None; sentry_traces_sample_rate: float = 0.1
    otel_service_name: str = "api"; otel_exporter_otlp_endpoint: str | None = None
    admin_enabled: bool = False; admin_read_only: bool = True; admin_staff_emails: str = ""

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def staff_emails(self) -> list[str]:
        return [e.strip().lower() for e in self.admin_staff_emails.split(",") if e.strip()]

    @property
    def sync_database_url(self) -> str:
        """Alembic and psql use the sync driver; everything else uses asyncpg."""
        return self.alembic_database_url or self.database_url.replace("+asyncpg", "+psycopg")

    @model_validator(mode="after")
    def _deployed_requirements(self) -> "Settings":
        if self.app_env == "local":
            return self
        missing = [n.upper() for n in
                   ("gcp_project_id", "kms_key_uri", "openai_api_key", "sentry_dsn")
                   if not getattr(self, n)]
        if self.storage_backend != "gcs":
            missing.append("STORAGE_BACKEND=gcs")
        if self.app_env == "production" and self.admin_enabled:
            missing.append("ADMIN_ENABLED=false (the admin never runs on api-prod)")
        if missing:
            raise ValueError(f"APP_ENV={self.app_env} also requires: {', '.join(missing)}")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        bad = ", ".join(".".join(map(str, e["loc"])).upper() or e["msg"] for e in exc.errors())
        raise SystemExit(f"FATAL: refusing to boot. Bad or missing env: {bad}\n{exc}") from exc


settings = get_settings()
