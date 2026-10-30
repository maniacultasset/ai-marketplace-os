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
  authorize/token URL or DASHBOARD_BASE_URL (S15). Off local, a JWT_SECRET (and, with the admin
  on, an ADMIN_SESSION_SECRET) must carry no whitespace and 32+ characters measured unstripped
  (audit PROD-1; a character-variety floor waits on TEAM-QUESTIONS T132). The api also needs
  https non-loopback ALLOWED_ORIGINS (exact origins, never `*`) and JWT_ISSUER (PROD-2).
  Numeric settings carry lower bounds in every env (PROD-3). A worker off local also needs the
  vision provider's key (B2): OpenAI, the only provider with a vision model.
- There is no module-level `settings`: call `get_settings()` where the value is needed.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Mapping
from functools import lru_cache
from importlib import metadata
from types import MappingProxyType
from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.model_prices import EMBEDDING_PRICE_USD_PER_MTOK, is_usable_openai_key

EMBEDDING_DIMENSIONS: Final[int] = 1536

# B2 (ai-deps C18): the env var holding each vision provider's key. DeepSeek has no vision model,
# so `LLM_PROVIDER_VISION` is OpenAI only, whatever `LLM_PROVIDER` says.
VisionProvider = Literal["openai"]
VISION_PROVIDER_KEY_ENV: Final[Mapping[str, str]] = MappingProxyType({"openai": "OPENAI_API_KEY"})

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
# DPP-1 (ADR-0021): the separate key for MFA secrets, which only the api may use.
MFA_KMS_KEY_URI_RE: Final = re.compile(
    r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/locations/asia-south1/"
    r"keyRings/marketplace-os/cryptoKeys/mfa-secret-key$"
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
# The same rule for the fields only the api reads: it signs tokens and answers browsers (PROD-2).
API_HTTPS_URLS: Final[tuple[tuple[str, str], ...]] = (("jwt_issuer", "JWT_ISSUER"),)
# A deployed secret (PROD-1): no whitespace anywhere and at least this many characters, measured
# on the raw value. A character-variety floor would also refuse CANON-ADDENDUM A§14's production
# fixture (`"x" * 32`), so it waits on TEAM-QUESTIONS T132.
SECRET_MIN_LENGTH: Final = 32

# Google sign-in (ADR-0020): Google's own endpoints. A deployed api refuses any other value (only
# APP_ENV=local may point them at the fake Google in tests and the e2e run).
GOOGLE_ENDPOINTS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        "google_authorize_url": (
            "GOOGLE_AUTHORIZE_URL",
            "https://accounts.google.com/o/oauth2/v2/auth",
        ),
        "google_token_url": ("GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token"),
        "google_jwks_url": ("GOOGLE_JWKS_URL", "https://www.googleapis.com/oauth2/v3/certs"),
        "google_issuer": ("GOOGLE_ISSUER", "https://accounts.google.com"),
    }
)
GOOGLE_CLIENT_ID_SUFFIX: Final = ".apps.googleusercontent.com"
GOOGLE_CALLBACK_PATH: Final = "/auth/google/callback"

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
    request_max_body_bytes: int = Field(default=10_485_760, ge=1)  # Cloud Run hard cap: 32 MiB
    rate_limit_per_tenant_per_min: int = Field(default=120, ge=1)
    rate_limit_per_ip_per_min: int = Field(default=60, ge=1)
    auth_login_rate_limit_per_min: int = Field(default=5, ge=1)
    auth_login_email_rate_limit_per_min: int = Field(default=10, ge=1)
    oauth_state_ttl_seconds: int = Field(default=600, ge=1)
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
    database_pool_size: int = Field(default=5, ge=1)
    database_max_overflow: int = Field(default=5, ge=0)
    database_echo: bool = False
    database_pool_timeout: int = Field(default=10, ge=1)
    database_pool_recycle: int = 1800
    db_statement_timeout_ms: int = Field(default=15_000, ge=1)  # 0 would disable it

    # ---- storage ----
    storage_backend: Literal["minio", "gcs"] = "minio"
    gcs_bucket_images: str = "marketplace-images"
    gcs_bucket_csv: str = "marketplace-csv"
    gcs_bucket_submissions: str = "marketplace-submissions"  # S21 D5: snapshots, raw bodies
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "local_only_minio_root_user"
    s3_secret_key: str = Field(default="local_only_minio_root_password_32_chars", repr=False)

    # ---- gcp ----
    gcp_project_id: str | None = None
    gcp_region: str = "asia-south1"
    kms_key_uri: str | None = None  # .../keyRings/marketplace-os/cryptoKeys/oauth-token-key
    mfa_kms_key_uri: str | None = None  # .../keyRings/marketplace-os/cryptoKeys/mfa-secret-key
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
    access_token_ttl_minutes: int = Field(default=30, ge=1)
    refresh_token_ttl_days: int = Field(default=14, ge=1)
    password_hash_scheme: Literal["argon2id"] = "argon2id"  # noqa: S105 - a scheme name
    mfa_enforcement: Literal["off", "all"] = "off"  # DPP-1: `off` keeps today's login and tokens
    mfa_grace_until: AwareDatetime | None = None  # ISO 8601 with an offset; naive is refused

    # ---- Google sign-in (ADR-0020): off by default; the secret comes from Secret Manager ----
    google_oauth_enabled: bool = False
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: SecretStr | None = None
    google_authorize_url: str = GOOGLE_ENDPOINTS["google_authorize_url"][1]
    google_token_url: str = GOOGLE_ENDPOINTS["google_token_url"][1]
    google_jwks_url: str = GOOGLE_ENDPOINTS["google_jwks_url"][1]
    google_issuer: str = GOOGLE_ENDPOINTS["google_issuer"][1]
    auth_google_rate_limit_per_min: int = Field(default=10, ge=1)

    # ---- models: per-provider keys ----
    llm_provider: Literal["openai", "deepseek"] = "openai"
    openai_api_key: str | None = Field(default=None, repr=False)
    deepseek_api_key: str | None = Field(default=None, repr=False)
    llm_model_default: str = "gpt-4o-mini"
    llm_model_escalation: str = "gpt-4o"
    llm_provider_vision: VisionProvider = "openai"
    llm_model_vision: str = Field(default="gpt-4o-mini", min_length=1, max_length=128)
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
    flipkart_enabled: bool = False  # S22 D1: Flipkart connect, refresh and adapter stay off

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
        "mfa_kms_key_uri",
        "mfa_grace_until",
        "amazon_app_id",
        "amazon_authorize_url",
        "flipkart_authorize_url",
        "google_oauth_client_id",
        "google_oauth_client_secret",
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

    @property
    def google_redirect_uri(self) -> str:
        """The one Google redirect URI (ADR-0020): derived from DASHBOARD_BASE_URL, so it can
        never drift from the origin the browser is on (the dashboard proxies `/api/*`). It is the
        exact string registered on the OAuth client in the Google Cloud console."""
        return f"{self.dashboard_base_url}{self.api_v1_prefix}{GOOGLE_CALLBACK_PATH}"

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


def _is_weak_secret(secret: str) -> bool:
    """Whitespace anywhere (a blank, padded or mangled Secret Manager value) or too short.
    Measured on the raw value: `min_length` counts the padding, `.strip()` would hide it."""
    return any(char.isspace() for char in secret) or len(secret) < SECRET_MIN_LENGTH


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


def _mfa_errors(s: Settings) -> list[str]:
    """DPP-1 (ADR-0021): a deployed api under `MFA_ENFORCEMENT=all` seals MFA secrets with its own
    key, never the token key; the worker has no grant on it and refuses the variable."""
    if s.app_env == "local":
        return []
    uri = s.mfa_kms_key_uri
    if s.app_role == "worker":
        return (
            ["APP_ROLE=worker refuses MFA_KMS_KEY_URI (the api alone holds that key)"]
            if uri
            else []
        )
    if s.app_role != "api":
        return []
    if uri is None:
        if s.mfa_enforcement == "all":
            return [f"APP_ENV={s.app_env} requires MFA_KMS_KEY_URI for MFA_ENFORCEMENT=all"]
        return []
    if not MFA_KMS_KEY_URI_RE.fullmatch(uri):
        return [
            "MFA_KMS_KEY_URI must be projects/<project>/locations/asia-south1/keyRings/"
            "marketplace-os/cryptoKeys/mfa-secret-key"
        ]
    if uri == s.kms_key_uri:
        return ["MFA_KMS_KEY_URI must differ from KMS_KEY_URI"]
    return []


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
    fields = DEPLOYED_HTTPS_URLS + (API_HTTPS_URLS if s.app_role == "api" else ())
    bad = []
    for field, name in fields:
        value = getattr(s, field)
        if value is not None and _not_deployable_url(value):
            bad.append(name)
    errors = [f"APP_ENV={s.app_env} refuses http or loopback {', '.join(bad)}"] if bad else []
    if s.app_role == "api" and not _deployable_origins(s.allowed_origins):
        errors.append(
            f"APP_ENV={s.app_env} refuses ALLOWED_ORIGINS that are not https non-loopback "
            "origins (no '*', path, query, userinfo or trailing '/')"
        )
    return errors


def _deployable_origins(raw: str) -> bool:
    """Every comma-separated entry an exact browser Origin (`https://host[:port]`), at least one
    (PROD-2): a `*`, an http or loopback host, or a trailing `/` (no browser sends one, so it
    silently grants nothing) is refused."""
    # the split `app.api.cors.parse_origins` applies (app.core never imports app.api)
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if not origins:
        return False
    for origin in origins:
        if origin == "*" or _not_deployable_url(origin):
            return False
        parts = urlsplit(origin)
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            return False
        if f"{parts.scheme}://{parts.netloc}" != origin:
            return False
    return True


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
    elif s.app_env != "local" and s.jwt_secret is not None and _is_weak_secret(s.jwt_secret):
        errors.append(
            f"APP_ENV={s.app_env} refuses a weak JWT_SECRET "
            f"(whitespace, or under {SECRET_MIN_LENGTH} characters)"
        )
    if s.app_env != "local" and s.app_role in ("api", "worker") and s.sentry_dsn is None:
        errors.append(f"APP_ENV={s.app_env} requires SENTRY_DSN for APP_ROLE={s.app_role}")
    # S23 (D9; ENV.md GIT_SHA): a deployed api or worker reports a real Sentry release
    if (
        s.app_env != "local"
        and s.app_role in ("api", "worker")
        and s.git_sha.strip() in ("", "dev")
    ):
        errors.append(
            f'APP_ENV={s.app_env} requires GIT_SHA (not empty, not "dev") for APP_ROLE={s.app_role}'
        )
    errors += _admin_errors(s)
    if s.app_env == "production" and s.app_role == "worker" and _pipeline_stub_installed():
        errors.append("APP_ENV=production APP_ROLE=worker refuses mkos-pipeline-stub")
    if s.log_level.upper() not in logging.getLevelNamesMapping():
        errors.append("LOG_LEVEL is not a logging level")
    errors += _kms_errors(s)
    errors += _mfa_errors(s)
    errors += _url_errors(s)
    errors += _storage_errors(s)
    errors += _embedding_errors(s)
    errors += _vision_errors(s)
    errors += _spapi_errors(s)
    errors += _flipkart_errors(s)
    errors += _google_errors(s)
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


def _vision_errors(s: Settings) -> list[str]:
    """B2 (ai-deps C18): a worker sends product images to `LLM_PROVIDER_VISION`, so outside local
    it needs that provider's key; the placeholder counts as none. Local uses the port's
    placeholder key. Names only, never the key or the model value."""
    if s.app_role != "worker" or s.app_env == "local":
        return []
    name = VISION_PROVIDER_KEY_ENV[s.llm_provider_vision]
    if is_usable_openai_key(getattr(s, name.lower())):
        return []
    return [
        f"LLM_PROVIDER_VISION={s.llm_provider_vision} requires {name} "
        "for APP_ROLE=worker outside APP_ENV=local"
    ]


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


PLACEHOLDER_VALUES: Final = frozenset({"", "replace_me"})


def _unset(value: str | None) -> bool:
    """S22 C11: unset, blank or the `.env.example` placeholder."""
    return value is None or value.strip().casefold() in PLACEHOLDER_VALUES


def _flipkart_errors(s: Settings) -> list[str]:
    """S22 D1, D8 (C10, C11): with `FLIPKART_ENABLED=true` off local, the api and the worker need
    real Flipkart credentials and the pinned API base and OAuth URLs. The flag off adds nothing.
    Names only, never a value."""
    if not s.flipkart_enabled or s.app_env == "local" or s.app_role not in ("api", "worker"):
        return []
    from app.adapters.flipkart.endpoint import (
        flipkart_endpoint_errors,
        flipkart_oauth_endpoint_errors,
    )

    errors: list[str] = []
    for name, value in (
        ("FLIPKART_CLIENT_ID", s.flipkart_client_id),
        ("FLIPKART_CLIENT_SECRET", s.flipkart_client_secret),
        ("FLIPKART_AUTHORIZE_URL", s.flipkart_authorize_url),
    ):
        if _unset(value):
            errors.append(f"FLIPKART_ENABLED=true requires {name}")
    errors += flipkart_endpoint_errors(s.flipkart_api_base, app_env=s.app_env)
    authorize = None if _unset(s.flipkart_authorize_url) else s.flipkart_authorize_url
    errors += flipkart_oauth_endpoint_errors(s.flipkart_token_url, authorize, app_env=s.app_env)
    return errors


def _google_errors(s: Settings) -> list[str]:
    """ADR-0020: Google's endpoints are fixed outside local (an override would let a fake
    issuer sign users in), and an api with `GOOGLE_OAUTH_ENABLED=true` needs the client id (a
    `*.apps.googleusercontent.com` id) and the secret (strong outside local). The flag off adds
    nothing, so a hotfix never needs the secret. Names only, never a value."""
    errors: list[str] = []
    if s.app_env != "local":
        changed = [env for field, (env, default) in GOOGLE_ENDPOINTS.items()
                   if getattr(s, field) != default]  # fmt: skip
        if changed:
            errors.append(f"APP_ENV={s.app_env} refuses overriding {', '.join(changed)}")
    if not s.google_oauth_enabled or s.app_role != "api":
        return errors
    client_id = s.google_oauth_client_id
    if _unset(client_id):
        errors.append("GOOGLE_OAUTH_ENABLED=true requires GOOGLE_OAUTH_CLIENT_ID")
    elif not (client_id or "").endswith(GOOGLE_CLIENT_ID_SUFFIX) and s.app_env != "local":
        errors.append(f"GOOGLE_OAUTH_CLIENT_ID must end with {GOOGLE_CLIENT_ID_SUFFIX}")
    held = s.google_oauth_client_secret
    secret = held.get_secret_value() if held is not None else None
    if _unset(secret):
        errors.append("GOOGLE_OAUTH_ENABLED=true requires GOOGLE_OAUTH_CLIENT_SECRET")
    elif s.app_env != "local" and _is_weak_secret(secret or ""):
        errors.append(
            f"APP_ENV={s.app_env} refuses a weak GOOGLE_OAUTH_CLIENT_SECRET "
            f"(whitespace, or under {SECRET_MIN_LENGTH} characters)"
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
    elif s.app_env != "local" and s.admin_enabled and _is_weak_secret(s.admin_session_secret):
        errors.append(
            f"APP_ENV={s.app_env} refuses a weak ADMIN_SESSION_SECRET "
            f"(whitespace, or under {SECRET_MIN_LENGTH} characters)"
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
