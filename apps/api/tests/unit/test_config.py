"""AC3: Settings (A§8-A§10 as amended by ADR-0013 §1-2), with the scaffold config.py defects fixed."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.deployed_env import DEPLOYED_S15_ENV

if TYPE_CHECKING:
    from app.core.config import Settings

LOCAL_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os"
CI_LITERAL = "ci_only_not_a_real_secret_at_least_32_chars"  # A§11:290
DEV_LITERAL = "dev_only_change_me_at_least_32_characters_long"  # ENV.md JWT_SECRET row
EXPORT_LITERAL = "local_only_schema_export_secret_32_chars"  # A§13:319
SOCKET_URL = (
    "postgresql+psycopg://app_rw:pw@/aimos?host=/cloudsql/aimos-staging:asia-south1:aimos-db"
)
FLOAT_FIELDS = (
    "SENTRY_TRACES_SAMPLE_RATE",
    "OTEL_TRACES_SAMPLER_ARG",
    "LLM_DAILY_BUDGET_USD",
    "LLM_TENANT_DAILY_BUDGET_USD",
)
SECRET_FIELDS = {
    "DATABASE_URL": "postgresql+psycopg://u:Zq9dbpass@localhost:5432/x",
    "ALEMBIC_DATABASE_URL": "postgresql+psycopg://m:Zq9alembicpass@localhost:5432/x",
    "JWT_SECRET": "Zq9jwt_" + "j" * 32,
    "ADMIN_SESSION_SECRET": "Zq9admin_" + "a" * 32,
    "S3_SECRET_KEY": "Zq9s3secret",
    "KMS_LOCAL_DEV_KEY": "Zq9kmslocal",
    "OPENAI_API_KEY": "Zq9openai",
    "DEEPSEEK_API_KEY": "Zq9deepseek",
    "AMAZON_CLIENT_SECRET": "Zq9amazon",
    "FLIPKART_CLIENT_SECRET": "Zq9flipkart",
    "SENTRY_DSN": "https://Zq9sentry@o0.ingest.sentry.io/0",
    "ALERT_SLACK_WEBHOOK": "https://hooks.slack.com/services/Zq9slack",
}
# S15: what a deployed api/worker also needs (the token key, an https dashboard origin).
DEPLOYED_S15 = DEPLOYED_S15_ENV
TEST_SENTRY_DSN = "https://public@o0.ingest.example.invalid/0"  # never resolvable (.invalid)
PRIVATE_JWT = "Zq9-not-committed-private-secret-32+"  # neither a Canon literal nor dev-prefixed
OTEL_SAMPLERS = (
    "always_on",
    "always_off",
    "traceidratio",
    "parentbased_always_on",
    "parentbased_always_off",
    "parentbased_traceidratio",
)
_ENV_TEXT = st.text(alphabet=st.characters(codec="utf-8", exclude_characters="\x00"))
# Property tests below set real env vars and build Settings; example timing varies with a cold
# import or coverage, so they run without Hypothesis's per-example deadline (no flaky timings).


def boot(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """Set `env` on top of the clean env and return a freshly built Settings."""
    from app.core.config import get_settings

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return get_settings()


def abort(monkeypatch: pytest.MonkeyPatch, **env: str) -> str:
    """Like boot(), but the boot must abort; returns the SystemExit message."""
    with pytest.raises(SystemExit) as info:
        boot(monkeypatch, **env)
    assert isinstance(info.value.code, str)
    assert info.value.code.startswith("FATAL: refusing to boot. ")
    return info.value.code


def _write_env_file(root: Path, lines: list[str]) -> None:
    (root / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize("key", ["NOT_A_FIELD", "ENV", "REDIS_URL", "NEXTAUTH_SECRET", "ÄPP_ENV"])
def test_unknown_env_file_key_aborts_and_names_it(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    _write_env_file(
        clean_env,
        ["APP_ENV=local", f"DATABASE_URL={LOCAL_URL}", f"JWT_SECRET={CI_LITERAL}", f"{key}=x1"],
    )
    message = abort(monkeypatch)
    assert key in message
    assert "extra_forbidden" in message


@pytest.mark.parametrize("role", ["api", "worker", "migrate", "seed", "shell"])
def test_missing_app_env_aborts_for_every_role(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    message = abort(
        monkeypatch,
        APP_ROLE=role,
        DATABASE_URL=LOCAL_URL,
        ALEMBIC_DATABASE_URL=LOCAL_URL,
        JWT_SECRET=CI_LITERAL,
    )
    assert "APP_ENV (missing)" in message


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({"APP_ROLE": "api", "JWT_SECRET": CI_LITERAL}, ["DATABASE_URL"]),
        ({"APP_ROLE": "api", "DATABASE_URL": LOCAL_URL}, ["JWT_SECRET"]),
        ({"APP_ROLE": "api"}, ["DATABASE_URL", "JWT_SECRET"]),
        ({"DATABASE_URL": LOCAL_URL}, ["JWT_SECRET"]),  # api is the default role
        ({"APP_ROLE": "worker", "JWT_SECRET": CI_LITERAL}, ["DATABASE_URL"]),
        ({"APP_ROLE": "migrate", "DATABASE_URL": LOCAL_URL}, ["ALEMBIC_DATABASE_URL"]),
        ({"APP_ROLE": "api", "DATABASE_URL": LOCAL_URL, "JWT_SECRET": CI_LITERAL}, []),
        ({"APP_ROLE": "worker", "DATABASE_URL": LOCAL_URL}, []),
        ({"APP_ROLE": "migrate", "ALEMBIC_DATABASE_URL": LOCAL_URL}, []),
        ({"APP_ROLE": "shell"}, []),
        # S9 D12: the seed role needs the app login and the migrator login.
        ({"APP_ROLE": "seed"}, ["DATABASE_URL", "ALEMBIC_DATABASE_URL"]),
        ({"APP_ROLE": "seed", "DATABASE_URL": LOCAL_URL}, ["ALEMBIC_DATABASE_URL"]),
        ({"APP_ROLE": "seed", "ALEMBIC_DATABASE_URL": LOCAL_URL}, ["DATABASE_URL"]),
        ({"APP_ROLE": "seed", "DATABASE_URL": LOCAL_URL, "ALEMBIC_DATABASE_URL": LOCAL_URL}, []),
    ],
)
def test_role_requirements(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], missing: list[str]
) -> None:
    if not missing:
        loaded = boot(monkeypatch, APP_ENV="local", **env)
        assert loaded.api_v1_prefix == "/api/v1"
        return
    message = abort(monkeypatch, APP_ENV="local", **env)
    role = env.get("APP_ROLE", "api")
    assert f"APP_ROLE={role} requires {', '.join(missing)}" in message
    for name in {"DATABASE_URL", "JWT_SECRET", "ALEMBIC_DATABASE_URL"} - set(missing):
        assert f" {name}" not in message.split("requires", 1)[1]


@pytest.mark.parametrize(
    ("name", "value"), [("JWT_SECRET", ""), ("DATABASE_URL", "   "), ("DATABASE_URL", "")]
)
def test_blank_required_value_counts_as_missing(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    env = {"APP_ENV": "local", "DATABASE_URL": LOCAL_URL, "JWT_SECRET": CI_LITERAL, name: value}
    message = abort(monkeypatch, **env)
    assert name in message


@pytest.mark.parametrize("role", ["bogus", "API", ""])
def test_unknown_role_aborts(clean_env: Path, monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    message = abort(
        monkeypatch, APP_ENV="local", APP_ROLE=role, DATABASE_URL=LOCAL_URL, JWT_SECRET=CI_LITERAL
    )
    assert "APP_ROLE (literal_error)" in message


@pytest.mark.parametrize("name", ["DATABASE_URL", "ALEMBIC_DATABASE_URL"])
@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("postgresql+asyncpg://postgres:postgres@localhost:5432/marketplace_os", False),
        ("postgresql://postgres:postgres@localhost:5432/marketplace_os", False),
        ("postgres://postgres:postgres@localhost:5432/marketplace_os", False),
        (LOCAL_URL, True),
        (SOCKET_URL, True),
    ],
)
def test_database_url_requires_psycopg(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, name: str, url: str, ok: bool
) -> None:
    env = {"APP_ENV": "local", "DATABASE_URL": LOCAL_URL, "JWT_SECRET": CI_LITERAL, name: url}
    if ok:
        assert getattr(boot(monkeypatch, **env), name.lower()) == url
    else:
        assert f"{name} (string_pattern_mismatch)" in abort(monkeypatch, **env)


@pytest.mark.parametrize(
    ("secret", "ok"), [("s" * 31, False), ("s" * 32, True), ("é" * 32, True), ("é" * 31, False)]
)
def test_jwt_secret_length_boundary(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, secret: str, ok: bool
) -> None:
    env = {"APP_ENV": "local", "DATABASE_URL": LOCAL_URL, "JWT_SECRET": secret}
    if ok:
        assert boot(monkeypatch, **env).jwt_secret == secret
    else:
        assert "JWT_SECRET (string_too_short)" in abort(monkeypatch, **env)


COMMITTED_VALUES = [
    DEV_LITERAL,
    CI_LITERAL,
    EXPORT_LITERAL,
    "dev_only_" + "a" * 32,
    "ci_only_" + "b" * 32,
    "local_only_" + "c" * 32,
    "DEV_ONLY_" + "a" * 32,
    " ci_only_" + "b" * 32,
]


@pytest.mark.parametrize("app_env", ["staging", "production", "local"])
@pytest.mark.parametrize("secret", [*COMMITTED_VALUES, "x" * 32])
def test_committed_jwt_secrets_refused_outside_local(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, app_env: str, secret: str
) -> None:
    env = {
        "APP_ENV": app_env,
        "DATABASE_URL": LOCAL_URL,
        "JWT_SECRET": secret,
        "SENTRY_DSN": TEST_SENTRY_DSN,
        **DEPLOYED_S15,
    }
    # A§14:338's "x" * 32 is the control: S14's production fixture must keep booting.
    if app_env == "local" or secret == "x" * 32:
        assert boot(monkeypatch, **env).jwt_secret == secret
        return
    message = abort(monkeypatch, **env)
    assert f"APP_ENV={app_env} refuses a committed or dev-prefixed JWT_SECRET" in message
    assert secret.strip() not in message
    assert secret.strip()[-32:] not in message


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("1536", True),
        (" 1536 ", True),
        (1536, True),
        ("1535", False),
        ("0", False),
        ("-1536", False),
        ("1536.0", False),
        ("+1536", False),
        ("01536", False),
        ("١٥٣٦", False),
        ("", False),
        ("NaN", False),
        ("Infinity", False),
        ("abc", False),
    ],
)
def test_embedding_dimensions_table(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, value: str | int, ok: bool
) -> None:
    if isinstance(value, int):
        from app.core.config import Settings

        direct = Settings(app_env="local", embedding_dimensions=value, _env_file=None)
        assert direct.embedding_dimensions == 1536
        return
    env = {"APP_ROLE": "shell", "APP_ENV": "local", "EMBEDDING_DIMENSIONS": value}
    if ok:
        assert boot(monkeypatch, **env).embedding_dimensions == 1536
    else:
        assert "EMBEDDING_DIMENSIONS (value_error)" in abort(monkeypatch, **env)


@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=100, deadline=None
)
@given(value=_ENV_TEXT.filter(lambda s: s.strip() != "1536"))
def test_embedding_dimensions_rejects_all_other_text(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    env = {"APP_ROLE": "shell", "APP_ENV": "local", "EMBEDDING_DIMENSIONS": value}
    assert "EMBEDDING_DIMENSIONS" in abort(monkeypatch, **env)


def test_otel_sampler_fields_load(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = boot(
        monkeypatch,
        APP_ROLE="shell",
        APP_ENV="local",
        OTEL_TRACES_SAMPLER="parentbased_traceidratio",
        OTEL_TRACES_SAMPLER_ARG="0.25",
    )
    assert loaded.otel_traces_sampler == "parentbased_traceidratio"
    assert loaded.otel_traces_sampler_arg == 0.25


@pytest.mark.parametrize("name", FLOAT_FIELDS)
@pytest.mark.parametrize("value", ["NaN", "nan", "inf", "-Infinity"])
def test_float_fields_reject_nan_and_infinity(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    message = abort(monkeypatch, APP_ROLE="shell", APP_ENV="local", **{name: value})
    assert name in message


@pytest.mark.parametrize("name", ["SENTRY_TRACES_SAMPLE_RATE", "OTEL_TRACES_SAMPLER_ARG"])
@pytest.mark.parametrize(
    ("value", "ok"), [("-0.01", False), ("1.01", False), ("0", True), ("1", True)]
)
def test_sample_rates_bounded(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str, ok: bool
) -> None:
    env = {"APP_ROLE": "shell", "APP_ENV": "local", name: value}
    if ok:
        assert getattr(boot(monkeypatch, **env), name.lower()) == float(value)
    else:
        assert name in abort(monkeypatch, **env)


@pytest.mark.parametrize(
    ("value", "ok"), [("0", False), ("65536", False), ("1", True), ("65535", True), ("8000", True)]
)
def test_port_bounds(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, value: str, ok: bool
) -> None:
    env = {"APP_ROLE": "shell", "APP_ENV": "local", "PORT": value}
    if ok:
        assert boot(monkeypatch, **env).port == int(value)
    else:
        assert "PORT" in abort(monkeypatch, **env)


def test_api_v1_prefix_pinned(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    message = abort(monkeypatch, APP_ROLE="shell", APP_ENV="local", API_V1_PREFIX="/v1")
    assert "API_V1_PREFIX (literal_error)" in message


def test_unicode_values_load(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_env_file(clean_env, ["APP_ROLE=shell", "APP_ENV=local", "ADMIN_LOCAL_EMAIL=dév@local"])
    assert boot(monkeypatch).admin_local_email == "dév@local"


def test_boot_error_never_echoes_values(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://postgres:Zq9SuperSecret@db/x")
    monkeypatch.setenv("JWT_SECRET", "Zq9short")
    with pytest.raises(SystemExit) as info:
        get_settings()
    message = str(info.value.code)
    assert "DATABASE_URL" in message
    assert "JWT_SECRET" in message
    assert "Zq9" not in message
    assert info.value.__context__ is None
    assert info.value.__cause__ is None

    # A role error built from a valid URL holding a password names only the variable.
    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://postgres:Zq9SuperSecret@db:5432/x")
    monkeypatch.delenv("JWT_SECRET")
    with pytest.raises(SystemExit) as info:
        get_settings()
    message = str(info.value.code)
    assert "APP_ROLE=api requires JWT_SECRET" in message
    for fragment in ("Zq9", "postgresql", "db:5432", "postgres:"):
        assert fragment not in message
    assert info.value.__context__ is None
    assert info.value.__cause__ is None


@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=100, deadline=None
)
@given(url_tail=_ENV_TEXT, jwt_tail=_ENV_TEXT)
def test_boot_error_never_echoes_generated_values(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, url_tail: str, jwt_tail: str
) -> None:
    from app.core.config import get_settings

    url, jwt = "Zq9" + url_tail, "Zq9" + jwt_tail
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("JWT_SECRET", jwt)
    get_settings.cache_clear()
    with pytest.raises(SystemExit) as info:
        get_settings()
    message = str(info.value.code)
    assert url not in message
    assert jwt not in message
    assert "Zq9" not in message
    assert info.value.__context__ is None


def test_settings_repr_hides_secrets(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = boot(monkeypatch, APP_ENV="local", **SECRET_FIELDS)
    rendered = repr(loaded) + str(loaded)
    for name, value in SECRET_FIELDS.items():
        assert getattr(loaded, name.lower()) == value
        assert value not in rendered, name
    assert "Zq9" not in rendered


def test_get_settings_cache_clear_rereads_env(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    first = boot(monkeypatch, APP_ROLE="shell", APP_ENV="local", LOG_LEVEL="INFO")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    assert get_settings() is first
    get_settings.cache_clear()
    assert get_settings().log_level == "DEBUG"


def test_no_module_level_settings_singleton(clean_env: Path) -> None:
    import app.core.config as config

    assert not hasattr(config, "settings")
    assert not [v for v in vars(config).values() if isinstance(v, config.Settings)]
    # Importing the module with no env at all must not abort (collection-time safety).
    env = {k: v for k, v in os.environ.items() if k not in {"APP_ENV", "DATABASE_URL"}}
    result = subprocess.run(
        [sys.executable, "-c", "import app.core.config"],
        cwd=clean_env,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_s3_defaults_equal_env_example(repo_root: Path) -> None:
    from app.core.config import Settings

    pairs = {}
    for raw in (repo_root / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        pairs[key] = value
    assert Settings.model_fields["s3_access_key"].default == pairs["S3_ACCESS_KEY"]
    assert Settings.model_fields["s3_secret_key"].default == pairs["S3_SECRET_KEY"]
    assert Settings.model_fields["s3_secret_key"].repr is False


def test_a9_defaults(ci_env: dict[str, str]) -> None:
    from app.core.config import get_settings

    loaded = get_settings()
    assert loaded.port == 8000
    assert loaded.otel_service_name == "marketplace-os-api"
    key = base64.b64decode(loaded.kms_local_dev_key, validate=True)
    assert key == b"local_dev_only_32_byte_key_00000"
    assert len(key) == 32


# ---- S11: the Sentry boot rule (CANON-ADDENDUM §14), sampler names, log level ----


def _deployed_env(role: str, app_env: str) -> dict[str, str]:
    env = {"APP_ENV": app_env, "APP_ROLE": role, "DATABASE_URL": LOCAL_URL, **DEPLOYED_S15}
    if role == "api":
        env["JWT_SECRET"] = PRIVATE_JWT
    return env


@pytest.mark.parametrize("app_env", ["staging", "production"])
@pytest.mark.parametrize("role", ["api", "worker"])
@pytest.mark.parametrize("dsn", [None, "", "  "])
def test_config_sentry_required(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, app_env: str, role: str, dsn: str | None
) -> None:
    env = _deployed_env(role, app_env)
    if dsn is not None:
        env["SENTRY_DSN"] = dsn
    message = abort(monkeypatch, **env)
    assert f"APP_ENV={app_env} requires SENTRY_DSN for APP_ROLE={role}" in message
    env["SENTRY_DSN"] = TEST_SENTRY_DSN
    if (app_env, role) == ("production", "worker"):
        # S14b CD2: this environment has the pipeline stub installed, which a production worker
        # refuses (tests/unit/test_config_boot.py); the SENTRY_DSN check itself is satisfied.
        message = abort(monkeypatch, **env)
        assert "requires SENTRY_DSN" not in message
        assert "refuses mkos-pipeline-stub" in message
        return
    assert boot(monkeypatch, **env).sentry_dsn == TEST_SENTRY_DSN


@pytest.mark.parametrize("role", ["migrate", "shell"])
def test_config_sentry_optional_roles(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    loaded = boot(monkeypatch, APP_ENV="staging", APP_ROLE=role, ALEMBIC_DATABASE_URL=LOCAL_URL)
    assert loaded.sentry_dsn is None


@pytest.mark.parametrize("dsn", ["", "   "])
def test_config_sentry_optional_local(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, dsn: str
) -> None:
    loaded = boot(
        monkeypatch, APP_ENV="local", DATABASE_URL=LOCAL_URL, JWT_SECRET=CI_LITERAL, SENTRY_DSN=dsn
    )
    assert loaded.sentry_dsn is None


def test_config_sentry_error_never_echoes_dsn(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A malformed DSN (no project id) next to a second boot error: names only, never the value.
    message = abort(
        monkeypatch,
        APP_ENV="staging",
        DATABASE_URL=LOCAL_URL,
        JWT_SECRET=CI_LITERAL,
        SENTRY_DSN="https://Zq9secret@x/",
    )
    assert "JWT_SECRET" in message
    assert "Zq9" not in message


@pytest.mark.parametrize("name", OTEL_SAMPLERS)
def test_otel_sampler_names(clean_env: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    loaded = boot(monkeypatch, APP_ROLE="shell", APP_ENV="local", OTEL_TRACES_SAMPLER=name)
    assert loaded.otel_traces_sampler == name


@pytest.mark.parametrize("name", ["ratio", "", "PARENTBASED_TRACEIDRATIO ", "jaeger_remote"])
def test_otel_sampler_unknown_aborts(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    message = abort(monkeypatch, APP_ROLE="shell", APP_ENV="local", OTEL_TRACES_SAMPLER=name)
    assert "OTEL_TRACES_SAMPLER" in message


@pytest.mark.parametrize("level", ["LOUD", "", " ", "TRACE"])
def test_log_level_unknown_aborts(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    message = abort(monkeypatch, APP_ROLE="shell", APP_ENV="local", LOG_LEVEL=level)
    assert "LOG_LEVEL is not a logging level" in message
    if level.strip():
        assert level not in message


@pytest.mark.parametrize("level", ["debug", "INFO", "Warning", "ERROR", "CRITICAL"])
def test_log_level_known_loads(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    assert boot(monkeypatch, APP_ROLE="shell", APP_ENV="local", LOG_LEVEL=level).log_level == level


@pytest.mark.parametrize("value", ["", "  "])
def test_blank_gcp_project_id_is_none(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # `.env.example` ships `GCP_PROJECT_ID=`: blank must not become `projects//traces/...`.
    loaded = boot(monkeypatch, APP_ROLE="shell", APP_ENV="local", GCP_PROJECT_ID=value)
    assert loaded.gcp_project_id is None


# ---- S15: the KMS boot rows, the dashboard origin and the deployed marketplace URLs ----

KMS_URI = (
    "projects/aimos-staging/locations/asia-south1/keyRings/marketplace-os/cryptoKeys/"
    "oauth-token-key"
)


def _deployed_kms_env(role: str, app_env: str = "staging") -> dict[str, str]:
    env = _deployed_env(role, app_env)
    env["SENTRY_DSN"] = TEST_SENTRY_DSN
    del env["KMS_KEY_URI"]
    return env


@pytest.mark.parametrize("role", ["api", "worker"])
def test_boot_kms_missing_uri_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    env = _deployed_kms_env(role)
    message = abort(monkeypatch, **env)
    assert f"APP_ENV=staging requires KMS_KEY_URI for APP_ROLE={role}" in message
    assert boot(monkeypatch, **env, KMS_KEY_URI=KMS_URI).kms_key_uri == KMS_URI


@pytest.mark.parametrize(
    "uri",
    [
        KMS_URI.replace("keyRings/marketplace-os", "keyRings/aimos"),
        KMS_URI.replace("cryptoKeys/oauth-token-key", "cryptoKeys/marketplace-tokens"),
    ],
    ids=["aimos", "marketplace-tokens"],
)
def test_boot_kms_forbidden_ring_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, uri: str
) -> None:
    message = abort(monkeypatch, **_deployed_kms_env("api"), KMS_KEY_URI=uri)
    assert "KMS_KEY_URI names a forbidden key ring or key" in message


@pytest.mark.parametrize(
    "uri",
    [
        KMS_URI.replace("asia-south1", "us-central1"),
        KMS_URI + "/cryptoKeyVersions/1",
        "projects/x/locations/asia-south1/keyRings/marketplace-os/cryptoKeys/oauth-token-key",
        " " + KMS_URI,
    ],
)
def test_boot_kms_wrong_region_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, uri: str
) -> None:
    message = abort(monkeypatch, **_deployed_kms_env("worker"), KMS_KEY_URI=uri)
    assert "KMS_KEY_URI must be projects/<project>/locations/asia-south1" in message


@pytest.mark.parametrize("role", ["api", "worker", "migrate", "shell"])
def test_boot_kms_local_key_refused_when_deployed(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    env = _deployed_kms_env(role) if role in ("api", "worker") else {"APP_ROLE": role}
    env.update(APP_ENV="production", KMS_KEY_URI=KMS_URI, ALEMBIC_DATABASE_URL=LOCAL_URL)
    message = abort(monkeypatch, **env, KMS_LOCAL_DEV_KEY="Zq9kmslocal")
    assert "APP_ENV=production refuses KMS_LOCAL_DEV_KEY" in message
    assert "Zq9" not in message


@pytest.mark.parametrize("role", ["migrate", "shell"])
def test_boot_kms_not_required_for_migrate_shell(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    loaded = boot(monkeypatch, APP_ENV="staging", APP_ROLE=role, ALEMBIC_DATABASE_URL=LOCAL_URL)
    assert loaded.kms_key_uri is None


@pytest.mark.parametrize("uri", ["", "  "])
def test_boot_kms_local_ok_without_uri(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, uri: str
) -> None:
    loaded = boot(
        monkeypatch,
        APP_ENV="local",
        APP_ROLE="worker",
        DATABASE_URL=LOCAL_URL,
        KMS_KEY_URI=uri,
        KMS_LOCAL_DEV_KEY="bG9jYWxfZGV2X29ubHlfMzJfYnl0ZV9rZXlfMDAwMDA=",
    )
    assert loaded.kms_key_uri is None
    assert loaded.dashboard_base_url == "http://localhost:3000"


def test_boot_kms_messages_hold_no_value(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uri = "projects/Zq9secretproj/locations/asia-south1/keyRings/aimos/cryptoKeys/k"
    env = _deployed_kms_env("api")
    env["DASHBOARD_BASE_URL"] = "http://Zq9dash.example"
    message = abort(
        monkeypatch,
        **env,
        KMS_KEY_URI=uri,
        KMS_LOCAL_DEV_KEY="Zq9localkey",
        AMAZON_AUTHORIZE_URL="http://127.0.0.1:4020/Zq9",
    )
    assert "KMS_KEY_URI" in message and "DASHBOARD_BASE_URL" in message
    assert "AMAZON_AUTHORIZE_URL" in message
    assert "Zq9" not in message


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:3000/marketplaces",
        "http://localhost:3000?next=x",
        "http://user:pw@localhost:3000",
        "http://localhost:3000#frag",
        "ftp://localhost:3000",
        "localhost:3000",
    ],
    ids=["path", "query", "userinfo", "fragment", "scheme", "no-scheme"],
)
def test_dashboard_base_url_origin_only(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    message = abort(
        monkeypatch,
        APP_ENV="local",
        DATABASE_URL=LOCAL_URL,
        JWT_SECRET=CI_LITERAL,
        DASHBOARD_BASE_URL=value,
    )
    assert "DASHBOARD_BASE_URL" in message


def test_dashboard_base_url_drops_trailing_slash(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = boot(
        monkeypatch,
        APP_ENV="local",
        DATABASE_URL=LOCAL_URL,
        JWT_SECRET=CI_LITERAL,
        DASHBOARD_BASE_URL="http://localhost:3130/",
    )
    assert loaded.dashboard_base_url == "http://localhost:3130"


@pytest.mark.parametrize(
    "name",
    [
        "AMAZON_AUTHORIZE_URL",
        "AMAZON_LWA_TOKEN_URL",
        "FLIPKART_AUTHORIZE_URL",
        "FLIPKART_TOKEN_URL",
        "AMAZON_OAUTH_REDIRECT_URI",
        "FLIPKART_OAUTH_REDIRECT_URI",
        "DASHBOARD_BASE_URL",
    ],
)
@pytest.mark.parametrize(
    "value", ["http://marketplace.example.invalid/x", "https://127.0.0.1/x", "https://localhost/x"]
)
def test_deployed_refuses_http_or_loopback_marketplace_urls(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    env = _deployed_kms_env("api") | {"KMS_KEY_URI": KMS_URI}
    if name == "DASHBOARD_BASE_URL":
        value = value.removesuffix("/x")
    env[name] = value
    message = abort(monkeypatch, **env)
    assert f"refuses http or loopback {name}" in message
    # the same value boots locally (the fake marketplace runs on 127.0.0.1)
    env["APP_ENV"] = "local"
    assert boot(monkeypatch, **env).app_env == "local"


@pytest.mark.parametrize("name", ["AMAZON_OAUTH_REDIRECT_URI", "FLIPKART_OAUTH_REDIRECT_URI"])
def test_deployed_refuses_the_loopback_redirect_default(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # An unset redirect URI falls back to the localhost default, which a deployed boot refuses.
    env = _deployed_kms_env("api") | {"KMS_KEY_URI": KMS_URI}
    del env[name]
    assert f"refuses http or loopback {name}" in abort(monkeypatch, **env)


def test_deployed_api_boots_with_https_urls(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _deployed_kms_env("api") | {
        "KMS_KEY_URI": KMS_URI,
        "AMAZON_AUTHORIZE_URL": "https://sellercentral.amazon.in/apps/authorize/consent",
    }
    loaded = boot(monkeypatch, **env)
    assert loaded.amazon_authorize_url is not None
    assert loaded.flipkart_authorize_url is None


# ---- S13b (KNOWN-ISSUES.md D): the object store outside local ----


@pytest.mark.parametrize("role", ["api", "worker"])
@pytest.mark.parametrize("name", ["S3_ACCESS_KEY", "S3_SECRET_KEY"])
@pytest.mark.parametrize(
    "value", [None, "local_only_minio_root_user", "  LOCAL_ONLY_x  ", "ci_only_key", "dev_only_k"]
)
def test_deployed_refuses_committed_s3_credentials(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, role: str, name: str, value: str | None
) -> None:
    env = _deployed_kms_env(role) | {"KMS_KEY_URI": KMS_URI}
    if role == "worker":
        env.pop("JWT_SECRET", None)
    if value is None:
        del env[name]  # unset: the committed `local_only_` default
    else:
        env[name] = value
    message = abort(monkeypatch, **env)
    assert f"refuses a committed or dev-prefixed {name}" in message
    assert "minio_root" not in message and "Zq9" not in message


@pytest.mark.parametrize(
    "value", [None, "http://storage.example.invalid", "https://127.0.0.1:9000", "https://localhost"]
)
def test_deployed_refuses_http_or_loopback_s3_endpoint(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    env = _deployed_kms_env("api") | {"KMS_KEY_URI": KMS_URI}
    if value is None:
        del env["S3_ENDPOINT_URL"]  # unset: the committed http://localhost:9000 default
    else:
        env["S3_ENDPOINT_URL"] = value
    assert "refuses http or loopback S3_ENDPOINT_URL" in abort(monkeypatch, **env)


def test_local_keeps_the_minio_defaults(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = boot(monkeypatch, APP_ENV="local", DATABASE_URL=LOCAL_URL, JWT_SECRET=CI_LITERAL)
    assert loaded.s3_access_key == "local_only_minio_root_user"
