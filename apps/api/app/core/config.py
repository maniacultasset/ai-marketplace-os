"""THE env register (docs/canon/ENV.md). Nothing else in the codebase reads the environment.

- One field per ENV.md row, in register order, and `extra="forbid"`: an unknown key in `.env`
  aborts the boot.
- `get_settings()` is the only place that constructs `Settings`. It aborts the process with a
  `SystemExit` whose message holds env-var names and error types only, never a value.
- Boot requirements depend on `APP_ROLE` (ADR-0013 §1): `api` needs DATABASE_URL and JWT_SECRET,
  `worker` needs DATABASE_URL, `migrate` needs ALEMBIC_DATABASE_URL, `shell` nothing more.
- There is no module-level `settings`: call `get_settings()` where the value is needed.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Final, Literal

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EMBEDDING_DIMENSIONS: Final[int] = 1536

# Every JWT literal committed to the repo or the Canon: ENV.md's JWT_SECRET row (.env.example),
# CANON-ADDENDUM.md:264 (the CI env block) and CANON-ADDENDUM.md:293 (the schema export).
COMMITTED_JWT_SECRETS: Final[frozenset[str]] = frozenset(
    {
        "dev_only_change_me_at_least_32_characters_long",
        "ci_only_not_a_real_secret_at_least_32_chars",
        "local_only_schema_export_secret_32_chars",
    }
)
# Their prefixes, matched after .strip().casefold(), so a padded or re-cased copy is refused too.
COMMITTED_JWT_SECRET_PREFIXES: Final[tuple[str, ...]] = ("dev_only", "ci_only", "local_only")

_PSYCOPG_URL: Final[str] = r"^postgresql\+psycopg://"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="forbid", case_sensitive=False
    )

    # ---- app ----
    app_env: Literal["local", "staging", "production"]
    app_role: Literal["api", "worker", "migrate", "shell"] = "api"
    app_name: str = "marketplace-os-api"
    api_v1_prefix: Literal["/api/v1"] = "/api/v1"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    allowed_origins: str = "http://localhost:3000"
    request_max_body_bytes: int = 10_485_760  # Cloud Run hard cap is 32 MiB
    rate_limit_per_tenant_per_min: int = 120
    rate_limit_per_ip_per_min: int = 60
    auth_login_rate_limit_per_min: int = 5
    oauth_state_ttl_seconds: int = 600
    admin_enabled: bool = False
    admin_base_url: str = "/internal/admin"
    admin_read_only: bool = True
    admin_session_secret: str = Field(
        default="dev_only_admin_session_secret_at_least_32_chars", min_length=32, repr=False
    )
    admin_staff_emails: str = ""
    admin_local_email: str = "dev@local"
    port: int = Field(default=8000, ge=1, le=65535)

    # ---- database (the pool is per process) ----
    database_url: str | None = Field(default=None, pattern=_PSYCOPG_URL, repr=False)
    alembic_database_url: str | None = Field(default=None, pattern=_PSYCOPG_URL, repr=False)
    database_pool_size: int = 5
    database_max_overflow: int = 5
    database_echo: bool = False
    database_pool_timeout: int = 10
    database_pool_recycle: int = 1800
    db_statement_timeout_ms: int = 15_000

    # ---- storage ----
    storage_backend: Literal["minio", "gcs"] = "minio"
    gcs_bucket_images: str = "marketplace-images"
    gcs_bucket_csv: str = "marketplace-csv"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = Field(default="minioadmin", repr=False)

    # ---- gcp ----
    gcp_project_id: str | None = None
    gcp_region: str = "asia-south1"
    kms_key_uri: str | None = None  # .../keyRings/marketplace-os/cryptoKeys/oauth-token-key
    kms_local_dev_key: str = Field(
        default="bG9jYWxfZGV2X29ubHlfMzJfYnl0ZV9rZXlfMDAwMDA=", repr=False  # LocalKmsStub only
    )
    secret_manager_enabled: bool = False
    git_sha: str = "dev"

    # ---- jwt ----
    jwt_secret: str | None = Field(default=None, min_length=32, repr=False)
    jwt_algorithm: Literal["HS256"] = "HS256"
    jwt_issuer: str = "http://localhost:8000"
    jwt_audience: str = "ai-marketplace-os"
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14
    password_hash_scheme: Literal["argon2id"] = "argon2id"  # noqa: S105 - a scheme name

    # ---- models: per-provider keys ----
    llm_provider: Literal["openai", "deepseek"] = "openai"
    openai_api_key: str | None = Field(default=None, repr=False)
    deepseek_api_key: str | None = Field(default=None, repr=False)
    llm_model_default: str = "gpt-4o-mini"
    llm_model_escalation: str = "gpt-4o"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = EMBEDDING_DIMENSIONS  # pinned: the vector(1536) column type
    llm_timeout_seconds: int = 60
    llm_max_correction_attempts: int = 3
    llm_daily_budget_usd: float = Field(default=12.0, allow_inf_nan=False)
    llm_tenant_daily_budget_usd: float = Field(default=2.5, allow_inf_nan=False)

    # ---- amazon: the redirect URI is the exact string filed in the application ----
    amazon_app_id: str | None = None
    amazon_client_id: str | None = None
    amazon_client_secret: str | None = Field(default=None, repr=False)
    amazon_lwa_token_url: str = "https://api.amazon.com/auth/o2/token"  # noqa: S105 - a URL
    amazon_spapi_endpoint: str = "https://sellingpartnerapi-eu.amazon.com"
    amazon_marketplace_id: str = "A21TJRUUN4KGV"
    amazon_oauth_redirect_uri: str = "http://localhost:8000/api/v1/marketplaces/amazon/callback"
    amazon_use_sandbox: bool = True

    # ---- flipkart ----
    flipkart_client_id: str | None = None
    flipkart_client_secret: str | None = Field(default=None, repr=False)
    flipkart_token_url: str = "https://api.flipkart.net/oauth-service/oauth/token"  # noqa: S105
    flipkart_api_base: str = "https://api.flipkart.net/sellers"
    flipkart_oauth_redirect_uri: str = "http://localhost:8000/api/v1/marketplaces/flipkart/callback"

    # ---- observability ----
    sentry_dsn: str | None = Field(default=None, repr=False)
    sentry_traces_sample_rate: float = Field(default=0.1, ge=0, le=1, allow_inf_nan=False)
    otel_service_name: str = "marketplace-os-api"
    otel_exporter_otlp_endpoint: str | None = None
    otel_traces_sampler: str = "parentbased_traceidratio"
    otel_traces_sampler_arg: float = Field(default=0.1, ge=0, le=1, allow_inf_nan=False)
    alert_slack_webhook: str | None = Field(default=None, repr=False)

    @field_validator("embedding_dimensions", mode="before")
    @classmethod
    def _pin_embedding_dimensions(cls, value: object) -> int:
        # The env string "1536" must load (the scaffold's Literal[1536] refused it), and nothing else may.
        if isinstance(value, int) and not isinstance(value, bool) and value == EMBEDDING_DIMENSIONS:
            return EMBEDDING_DIMENSIONS
        if isinstance(value, str) and value.strip() == str(EMBEDDING_DIMENSIONS):
            return EMBEDDING_DIMENSIONS
        raise ValueError("must be exactly 1536")


def _is_committed_jwt_secret(secret: str) -> bool:
    folded = secret.strip().casefold()
    return secret.strip() in COMMITTED_JWT_SECRETS or folded.startswith(
        COMMITTED_JWT_SECRET_PREFIXES
    )


def _boot_errors(s: Settings) -> list[str]:
    """Role and deployment checks, run after construction. Messages are built from env-var
    names and the APP_ROLE/APP_ENV literals only, never from a field value."""
    present = {
        "DATABASE_URL": s.database_url is not None,
        "JWT_SECRET": s.jwt_secret is not None,
        "ALEMBIC_DATABASE_URL": s.alembic_database_url is not None,
    }
    required = {
        "api": ("DATABASE_URL", "JWT_SECRET"),
        "worker": ("DATABASE_URL",),
        "migrate": ("ALEMBIC_DATABASE_URL",),
        "shell": (),
    }[s.app_role]
    errors: list[str] = []
    missing = [name for name in required if not present[name]]
    if missing:
        errors.append(f"APP_ROLE={s.app_role} requires {', '.join(missing)}")
    if s.app_env != "local" and s.jwt_secret is not None and _is_committed_jwt_secret(s.jwt_secret):
        errors.append(f"APP_ENV={s.app_env} refuses a committed or dev-prefixed JWT_SECRET")
    return errors


def _summarise(exc: ValidationError) -> str:
    """Names and error types only: never `input`, `msg` or `str(exc)`, which echo values."""
    parts = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        name = ".".join(str(part) for part in error["loc"]).upper() or "SETTINGS"
        parts.append(f"{name} ({error['type']})")
    return ", ".join(parts)


def _load() -> Settings | str:
    try:
        return Settings()
    except ValidationError as exc:
        return _summarise(exc)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # _load() returns the summary instead of raising, so the SystemExit below is raised outside
    # any `except` block: its __context__ and __cause__ stay None, and the ValidationError (whose
    # `input` holds values) is unreachable from it.
    loaded = _load()
    if isinstance(loaded, str):
        raise SystemExit(f"FATAL: refusing to boot. Bad or missing env: {loaded}")
    errors = _boot_errors(loaded)
    if errors:
        raise SystemExit("FATAL: refusing to boot. " + "; ".join(errors))
    return loaded
