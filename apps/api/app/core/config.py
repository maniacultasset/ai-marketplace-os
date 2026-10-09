"""THE env register (docs/canon/ENV.md). Nothing else in the codebase reads the environment.

- One field per ENV.md row, in register order, and `extra="forbid"`: an unknown key in `.env`
  aborts the boot.
- `get_settings()` is the only place that constructs `Settings`. It aborts the process with a
  `SystemExit` whose message holds env-var names and error types only, never a value.
- Boot requirements depend on `APP_ROLE` (ADR-0013 §1): `api` needs DATABASE_URL and JWT_SECRET,
  `worker` needs DATABASE_URL, `migrate` needs ALEMBIC_DATABASE_URL, `shell` nothing more.
  Outside `APP_ENV=local`, `api` and `worker` also need SENTRY_DSN (CANON-ADDENDUM.md §14); a
  blank value counts as missing. They also need a KMS_KEY_URI naming the one token key
  (CANON.md:70), refuse KMS_LOCAL_DEV_KEY (any role), and refuse an http or loopback marketplace
  authorize/token URL or DASHBOARD_BASE_URL (S15).
- There is no module-level `settings`: call `get_settings()` where the value is needed.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from functools import lru_cache
from importlib import metadata
from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.model_prices import EMBEDDING_PRICE_USD_PER_MTOK, is_usable_openai_key

EMBEDDING_DIMENSIONS: Final[int] = 1536

# Every JWT literal committed to the repo or the Canon: ENV.md's JWT_SECRET row (.env.example),
# CANON-ADDENDUM.md:290 (the CI env block) and CANON-ADDENDUM.md:319 (the schema export).
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
# The in-repo pipeline stub's distribution (S14b CD2). A production worker refuses to run it; the
# check reads the installed metadata only, it never imports `mkos_pipeline`.
PIPELINE_STUB_DISTRIBUTION: Final[str] = "mkos-pipeline-stub"

# CANON.md:70: the one token key outside local (S15). The kickoff pack's `aimos` ring and
# `marketplace-tokens` key are forbidden by name (KNOWN-ISSUES.md:13).
KMS_KEY_URI_RE: Final = re.compile(
    r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/locations/asia-south1/"
    r"keyRings/marketplace-os/cryptoKeys/oauth-token-key$"
)
FORBIDDEN_KMS_PARTS: Final[tuple[str, ...]] = ("keyRings/aimos", "cryptoKeys/marketplace-tokens")
# Settings field -> env-var name: the marketplace, OAuth redirect and dashboard URLs a deployed
# api/worker must serve over https on a non-loopback host (the redirect defaults are loopback).
DEPLOYED_HTTPS_URLS: Final[tuple[tuple[str, str], ...]] = (
    ("amazon_authorize_url", "AMAZON_AUTHORIZE_URL"),
    ("amazon_lwa_token_url", "AMAZON_LWA_TOKEN_URL"),
    ("flipkart_authorize_url", "FLIPKART_AUTHORIZE_URL"),
    ("flipkart_token_url", "FLIPKART_TOKEN_URL"),
    ("amazon_oauth_redirect_uri", "AMAZON_OAUTH_REDIRECT_URI"),
    ("flipkart_oauth_redirect_uri", "FLIPKART_OAUTH_REDIRECT_URI"),
    ("dashboard_base_url", "DASHBOARD_BASE_URL"),
)

OtelSampler = Literal[
    "always_on",
    "always_off",
    "traceidratio",
    "parentbased_always_on",
    "parentbased_always_off",
    "parentbased_traceidratio",
]
"""The OpenTelemetry SDK's built-in sampler names (OTEL_TRACES_SAMPLER)."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="forbid", case_sensitive=False
    )

    # ---- app ----
    app_env: Literal["local", "staging", "production"]
    app_role: Literal["api", "worker", "migrate", "seed", "shell"] = "api"
    app_name: str = "marketplace-os-api"
    api_v1_prefix: Literal["/api/v1"] = "/api/v1"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    allowed_origins: str = "http://localhost:3000"
    dashboard_base_url: str = "http://localhost:3000"  # an origin: the OAuth callback's 303 target
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
    s3_access_key: str = "local_only_minio_root_user"
    s3_secret_key: str = Field(default="local_only_minio_root_password_32_chars", repr=False)

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
    amazon_authorize_url: str | None = None  # the consent page; unset: connect answers 501
    amazon_lwa_token_url: str = "https://api.amazon.com/auth/o2/token"  # noqa: S105 - a URL
    amazon_spapi_endpoint: str = "https://sellingpartnerapi-eu.amazon.com"
    amazon_marketplace_id: str = "A21TJRUUN4KGV"
    amazon_oauth_redirect_uri: str = "http://localhost:8000/api/v1/marketplaces/amazon/callback"
    amazon_use_sandbox: bool = True

    # ---- flipkart ----
    flipkart_client_id: str | None = None
    flipkart_client_secret: str | None = Field(default=None, repr=False)
    flipkart_authorize_url: str | None = None
    flipkart_token_url: str = "https://api.flipkart.net/oauth-service/oauth/token"  # noqa: S105
    flipkart_api_base: str = "https://api.flipkart.net/sellers"
    flipkart_oauth_redirect_uri: str = "http://localhost:8000/api/v1/marketplaces/flipkart/callback"

    # ---- observability ----
    sentry_dsn: str | None = Field(default=None, repr=False)
    sentry_traces_sample_rate: float = Field(default=0.1, ge=0, le=1, allow_inf_nan=False)
    otel_service_name: str = "marketplace-os-api"
    otel_exporter_otlp_endpoint: str | None = None
    otel_traces_sampler: OtelSampler = "parentbased_traceidratio"
    otel_traces_sampler_arg: float = Field(default=0.1, ge=0, le=1, allow_inf_nan=False)
    alert_slack_webhook: str | None = Field(default=None, repr=False)

    @field_validator(
        "sentry_dsn",
        "gcp_project_id",
        "kms_key_uri",
        "amazon_app_id",
        "amazon_authorize_url",
        "flipkart_authorize_url",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        # `.env.example` ships `SENTRY_DSN=`, `GCP_PROJECT_ID=`, `KMS_KEY_URI=` and
        # `AMAZON_APP_ID=`: blank means unset.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("dashboard_base_url")
    @classmethod
    def _origin_only(cls, value: str) -> str:
        # The callback builds `{origin}/marketplaces/connected?pending=…`: scheme, host and port
        # only (a trailing `/` is dropped), never a path, query, fragment or userinfo.
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("must be an http(s) origin")
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise ValueError("must not carry userinfo")
        if parts.path not in ("", "/") or parts.query or parts.fragment or "#" in value:
            raise ValueError("must be an origin: no path, query or fragment")
        return f"{parts.scheme}://{parts.netloc}"

    @field_validator("embedding_dimensions", mode="before")
    @classmethod
    def _pin_embedding_dimensions(cls, value: object) -> int:
        # The env string "1536" must load (the scaffold's Literal[1536] refused it), and nothing else may.
        if isinstance(value, int) and not isinstance(value, bool) and value == EMBEDDING_DIMENSIONS:
            return EMBEDDING_DIMENSIONS
        if isinstance(value, str) and value.strip() == str(EMBEDDING_DIMENSIONS):
            return EMBEDDING_DIMENSIONS
        raise ValueError("must be exactly 1536")


# The committed admin session literal (ENV.md ADMIN_SESSION_SECRET row, `.env.example`).
COMMITTED_ADMIN_SECRETS: Final[frozenset[str]] = frozenset(
    {"dev_only_admin_session_secret_at_least_32_chars"}
)
ADMIN_MOUNT_PATH: Final[str] = "/internal/admin"


def _is_committed_secret(secret: str, committed: frozenset[str]) -> bool:
    folded = secret.strip().casefold()
    return secret.strip() in committed or folded.startswith(COMMITTED_JWT_SECRET_PREFIXES)


def _is_committed_jwt_secret(secret: str) -> bool:
    return _is_committed_secret(secret, COMMITTED_JWT_SECRETS)


def _is_loopback_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _not_deployable_url(value: str) -> bool:
    parts = urlsplit(value.strip())
    host = (parts.hostname or "").lower()
    return parts.scheme != "https" or not host or _is_loopback_host(host)


def _kms_errors(s: Settings) -> list[str]:
    """CANON.md:68-70 and ENV.md:60-61: a deployed api or worker seals tokens with Cloud KMS,
    never the LocalKmsStub key."""
    errors: list[str] = []
    if s.app_env == "local":
        return errors
    if "kms_local_dev_key" in s.model_fields_set:
        errors.append(f"APP_ENV={s.app_env} refuses KMS_LOCAL_DEV_KEY (LocalKmsStub only)")
    if s.app_role not in ("api", "worker"):
        return errors
    uri = s.kms_key_uri
    if uri is None:
        errors.append(f"APP_ENV={s.app_env} requires KMS_KEY_URI for APP_ROLE={s.app_role}")
    elif any(part in uri for part in FORBIDDEN_KMS_PARTS):
        errors.append("KMS_KEY_URI names a forbidden key ring or key (use marketplace-os)")
    elif not KMS_KEY_URI_RE.fullmatch(uri):
        errors.append(
            "KMS_KEY_URI must be projects/<project>/locations/asia-south1/keyRings/"
            "marketplace-os/cryptoKeys/oauth-token-key"
        )
    return errors


def _storage_errors(s: Settings) -> list[str]:
    """KNOWN-ISSUES.md D: outside local, an api or worker refuses the committed `local_only_` MinIO
    credentials and a non-https or loopback object-store endpoint (S13b, the step adding S3)."""
    if s.app_env == "local" or s.app_role not in ("api", "worker"):
        return []
    errors: list[str] = []
    for name, value in (("S3_ACCESS_KEY", s.s3_access_key), ("S3_SECRET_KEY", s.s3_secret_key)):
        if _is_committed_secret(value, frozenset()):
            errors.append(f"APP_ENV={s.app_env} refuses a committed or dev-prefixed {name}")
    if _not_deployable_url(s.s3_endpoint_url):
        errors.append(f"APP_ENV={s.app_env} refuses http or loopback S3_ENDPOINT_URL")
    return errors


def _url_errors(s: Settings) -> list[str]:
    if s.app_env == "local" or s.app_role not in ("api", "worker"):
        return []
    bad = []
    for field, name in DEPLOYED_HTTPS_URLS:
        value = getattr(s, field)
        if value is not None and _not_deployable_url(value):
            bad.append(name)
    if not bad:
        return []
    return [f"APP_ENV={s.app_env} refuses http or loopback {', '.join(bad)}"]


def _pipeline_stub_installed() -> bool:
    try:
        metadata.distribution(PIPELINE_STUB_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return False
    return True


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
        "seed": ("DATABASE_URL", "ALEMBIC_DATABASE_URL"),
        "shell": (),
    }[s.app_role]
    errors: list[str] = []
    missing = [name for name in required if not present[name]]
    if missing:
        errors.append(f"APP_ROLE={s.app_role} requires {', '.join(missing)}")
    if s.app_env != "local" and s.jwt_secret is not None and _is_committed_jwt_secret(s.jwt_secret):
        errors.append(f"APP_ENV={s.app_env} refuses a committed or dev-prefixed JWT_SECRET")
    if s.app_env != "local" and s.app_role in ("api", "worker") and s.sentry_dsn is None:
        errors.append(f"APP_ENV={s.app_env} requires SENTRY_DSN for APP_ROLE={s.app_role}")
    errors += _admin_errors(s)
    if s.app_env == "production" and s.app_role == "worker" and _pipeline_stub_installed():
        errors.append("APP_ENV=production APP_ROLE=worker refuses mkos-pipeline-stub")
    if s.log_level.upper() not in logging.getLevelNamesMapping():
        errors.append("LOG_LEVEL is not a logging level")
    errors += _kms_errors(s)
    errors += _url_errors(s)
    errors += _storage_errors(s)
    errors += _embedding_errors(s)
    errors += _spapi_errors(s)
    return errors


def _embedding_errors(s: Settings) -> list[str]:
    """S16 PLAN D4/D5 (ADR-0013 §1: OpenAI at S16): a worker embeds, so outside local it needs a
    real key (never hash vectors under the real model name), and every worker needs a price for
    its embedding model. Names only, never the key or the model value."""
    if s.app_role != "worker":
        return []
    errors: list[str] = []
    if s.app_env != "local" and not is_usable_openai_key(s.openai_api_key):
        errors.append("APP_ROLE=worker requires OPENAI_API_KEY outside APP_ENV=local")
    if s.embedding_model not in EMBEDDING_PRICE_USD_PER_MTOK:
        errors.append("EMBEDDING_MODEL has no price entry")
    return errors


def _spapi_errors(s: Settings) -> list[str]:
    """S20 C17 (ENV.md:101-104): the sandbox flag is false only in production, and off local the
    SP-API host is pinned to the flag (the api and the worker, the roles that reach it)."""
    from app.adapters.amazon.endpoint import spapi_endpoint_errors

    errors: list[str] = []
    if not s.amazon_use_sandbox and s.app_env != "production":
        errors.append(f"APP_ENV={s.app_env} refuses AMAZON_USE_SANDBOX=false")
    if s.app_env != "local" and s.app_role in ("api", "worker"):
        errors += spapi_endpoint_errors(
            s.amazon_spapi_endpoint, use_sandbox=s.amazon_use_sandbox, app_env=s.app_env
        )
    return errors


def _admin_errors(s: Settings) -> list[str]:
    """The `/internal/admin` guards (ENV.md:22-27; ADR-0013 §1; S14b D10): never in production, a
    real session secret outside local, read-only in v1 (requeue is the staff CLI), one mount path.
    """
    errors: list[str] = []
    if s.app_env == "production" and s.admin_enabled:
        errors.append("APP_ENV=production refuses ADMIN_ENABLED=true")
    if (
        s.app_env != "local"
        and s.admin_enabled
        and _is_committed_secret(s.admin_session_secret, COMMITTED_ADMIN_SECRETS)
    ):
        errors.append(
            f"APP_ENV={s.app_env} refuses a committed or dev-prefixed ADMIN_SESSION_SECRET"
        )
    if not s.admin_read_only:
        errors.append("ADMIN_READ_ONLY=false is refused in v1")
    if s.admin_base_url != ADMIN_MOUNT_PATH:
        errors.append(f"ADMIN_BASE_URL must be {ADMIN_MOUNT_PATH}")
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


# The process variables a child process may inherit (S17b: the image check child). Only these
# names are ever read, so no DSN, token, key or secret reaches the child.
CHILD_PROCESS_ENV: Final = ("PATH", "PYTHONPATH", "VIRTUAL_ENV")


def child_process_env() -> dict[str, str]:
    """The allow-listed process variables that are set (R1: env access stays in this module)."""
    import os

    return {name: os.environ[name] for name in CHILD_PROCESS_ENV if name in os.environ}
