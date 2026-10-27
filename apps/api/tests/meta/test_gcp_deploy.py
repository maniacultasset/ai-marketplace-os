"""The by-hand GCP deploy toolkit (scripts/deploy/gcp/, docs/runbooks/DEPLOY-GCP.md) and its
Cloud SQL bootstrap runner (scripts/db/run_bootstrap.py). Nothing here contacts Google Cloud: the
toolkit runs under --dry-run with a fake `gcloud` that records every call but a local `--help`."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

DEPLOY = "scripts/deploy/gcp"
SHA = "0123456789ab"
ORIGIN = "https://app-staging.example.com"
API_URL = "https://api-staging.example.com"
SUBCOMMANDS = (
    "preflight",
    "setup",
    "build",
    "db-init",
    "migrate",
    "deploy",
    "release",
    "smoke",
    "tenant",
    "status",
)
# Dummy secret values a deployed process would get from Secret Manager (never real ones).
SECRET_VALUES = {
    "DATABASE_URL": "postgresql+psycopg://app_rw:" + "a" * 48 + "@/marketplace_os"
    "?host=/cloudsql/aimos-staging:asia-south1:aimos-db",
    "JWT_SECRET": "b" * 64,
    "ADMIN_SESSION_SECRET": "c" * 64,
    "S3_ACCESS_KEY": "GOOG1EXAMPLEACCESSID",
    "S3_SECRET_KEY": "d" * 40,
    "SENTRY_DSN": "https://key@o0.ingest.sentry.io/0",
    "OPENAI_API_KEY": "sk-test-" + "e" * 32,
}

FAKE_GCLOUD = """#!/bin/sh
case " $* " in
  *" --help "*) [ -n "$FAKE_WORKER_POOLS_DEPLOY" ] || case "$*" in "run worker-pools deploy"*) exit 1 ;; esac; exit 0 ;;
esac
echo "$*" >> "$FAKE_GCLOUD_LOG"
exit 99
"""


@pytest.fixture
def toolkit(repo_root: Path, tmp_path: Path) -> Iterator[Path]:
    """A copy of scripts/deploy/gcp with the origins and API URL filled in."""
    target = tmp_path / "repo" / DEPLOY
    target.mkdir(parents=True)
    for source in (repo_root / DEPLOY).iterdir():
        text = source.read_text(encoding="utf-8")
        if source.suffix == ".env":
            text = text.replace("__FILL_ME__", ORIGIN).replace(
                "API_PUBLIC_URL=\n", f"API_PUBLIC_URL={API_URL}\n"
            )
        (target / source.name).write_text(text, encoding="utf-8")
    (target / "deploy.sh").chmod(0o755)
    yield target


def _run(
    toolkit: Path, tmp_path: Path, *args: str, worker_deploy: bool = False
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "gcloud"
    fake.write_text(FAKE_GCLOUD, encoding="utf-8")
    fake.chmod(0o755)
    log = tmp_path / "gcloud.log"
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "FAKE_GCLOUD_LOG": str(log),
        "FAKE_WORKER_POOLS_DEPLOY": "1" if worker_deploy else "",
    }
    result = subprocess.run(
        ["bash", str(toolkit / "deploy.sh"), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
        cwd=tmp_path,
    )
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    assert calls == "", f"gcloud ran under --dry-run: {calls}"
    return result


def _blocks(output: str, header: str) -> list[str]:
    """The indented bodies deploy.sh prints after each `deploy: <header> <path>:` line."""
    blocks: list[str] = []
    lines = output.splitlines()
    for start, line in enumerate(lines):
        if not line.startswith(f"deploy: {header} "):
            continue
        body = []
        for inner in lines[start + 1 :]:
            if not inner.startswith("    "):
                break
            body.append(inner[4:])
        blocks.append("\n".join(body))
    assert blocks, header
    return blocks


def _api_env(output: str) -> dict[str, str]:
    loaded = yaml.safe_load(_blocks(output, "env file")[0])
    assert isinstance(loaded, dict)
    return {str(k): str(v) for k, v in loaded.items()}


def _worker_spec(output: str) -> dict[str, object]:
    spec = yaml.safe_load(_blocks(output, "worker pool spec")[0])
    assert isinstance(spec, dict)
    return spec


def _secret_names(command_line: str) -> list[str]:
    match = re.search(r"--set-secrets=(\S+)", command_line)
    assert match is not None, command_line
    return [ref.split("=", 1)[0] for ref in match.group(1).replace("\\,", ",").split(",")]


# ---- the script ----


def test_deploy_script_parses_and_fails_fast(repo_root: Path) -> None:
    script = repo_root / DEPLOY / "deploy.sh"
    assert os.access(script, os.X_OK)
    lines = script.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#!/usr/bin/env bash"
    assert "set -euo pipefail" in lines[:40]
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_usage_lists_every_subcommand(toolkit: Path, tmp_path: Path) -> None:
    result = _run(toolkit, tmp_path, "staging", "nope", "--dry-run")
    assert result.returncode == 2
    for name in SUBCOMMANDS:
        assert re.search(rf"^  {re.escape(name)} ", result.stderr, re.MULTILINE), name


@pytest.mark.parametrize("env_name", ["staging", "prod"])
@pytest.mark.parametrize(
    "args",
    [
        ("preflight",),
        ("setup",),
        ("db-init",),
        ("migrate",),
        ("deploy",),
        ("smoke",),
        ("status",),
        (
            "tenant",
            "create",
            "--name",
            "Acme Co",
            "--slug",
            "acme",
            "--owner-email",
            "o@a.test",
            "--staff-email",
            "s@a.test",
        ),
        (
            "tenant",
            "add-member",
            "--slug",
            "acme",
            "--email",
            "r@a.test",
            "--role",
            "reviewer",
            "--staff-email",
            "s@a.test",
        ),
        ("tenant", "set-password", "--email", "o@a.test", "--staff-email", "s@a.test"),
    ],
)
def test_dry_run_runs_no_gcloud(
    toolkit: Path, tmp_path: Path, env_name: str, args: tuple[str, ...]
) -> None:
    result = _run(toolkit, tmp_path, env_name, *args, "--dry-run", "--sha", SHA)
    assert result.returncode == 0, result.stderr
    assert re.search(r"^\+ (gcloud|curl) ", result.stdout + result.stderr, re.MULTILINE)


def test_setup_refuses_unfilled_origins(repo_root: Path, tmp_path: Path) -> None:
    result = _run(repo_root / DEPLOY, tmp_path, "staging", "setup", "--dry-run", "--sha", SHA)
    assert result.returncode == 1
    assert "ALLOWED_ORIGINS is not filled" in result.stderr


def test_setup_generates_secrets_without_values(toolkit: Path, tmp_path: Path) -> None:
    out = _run(toolkit, tmp_path, "staging", "setup", "--dry-run", "--sha", SHA).stdout
    for name, size in (("JWT_SECRET", 32), ("ADMIN_SESSION_SECRET", 32), ("POSTGRES_PASSWORD", 24)):
        assert (
            f"+ openssl rand -hex {size} | gcloud secrets versions add {name} --data-file=-" in out
        )
    for name in ("APP_RW_PASSWORD", "APP_RO_PASSWORD", "MIGRATOR_PASSWORD"):
        assert f"gcloud secrets versions add {name} --data-file=-" in out
    assert (
        "postgresql+psycopg://app_rw:<APP_RW_PASSWORD>@/marketplace_os"
        "?host=/cloudsql/aimos-staging:asia-south1:aimos-db" in out
    )
    assert "--replication-policy=user-managed --locations=asia-south1" in out
    assert "--flags-file=<0600 temp file holding POSTGRES_PASSWORD>" in out
    # the user-provided secrets: a read -s command for the user's own terminal, then a stop
    for name in ("SENTRY_DSN", "OPENAI_API_KEY"):
        assert f'bash -c \'read -rsp "{name}: " v; echo; printf %s "$v" | ' in out
    # least privilege (ADR-0018): the api encrypts only, the worker encrypts and decrypts; no
    # other account touches the token key; secrets granted one by one
    kms = [
        line
        for line in out.splitlines()
        if "kms keys add-iam-policy-binding oauth-token-key " in line
    ]
    assert len(kms) == 2
    (api,) = [line for line in kms if "aimos-api@" in line]
    (worker,) = [line for line in kms if "aimos-worker@" in line]
    assert "--role=roles/cloudkms.cryptoKeyEncrypter " in api
    assert "--role=roles/cloudkms.cryptoKeyEncrypterDecrypter " in worker
    assert "roles/secretmanager.secretAccessor" not in "".join(
        line for line in out.splitlines() if "projects add-iam-policy-binding" in line
    )
    sql = next(line for line in out.splitlines() if "sql instances create" in line)
    for flag in (
        "--database-version=POSTGRES_17",
        "--edition=ENTERPRISE",
        "--tier=db-custom-1-3840",
        "--enable-point-in-time-recovery",
        "--retained-transaction-log-days=7",
        "--connector-enforcement=REQUIRED",
        "log_min_error_statement=panic",
        "pg_stat_statements.track_utility=off",
    ):
        assert flag in sql, flag


def test_prod_setup_sizes(toolkit: Path, tmp_path: Path) -> None:
    out = _run(toolkit, tmp_path, "prod", "setup", "--dry-run", "--sha", SHA).stdout
    sql = next(line for line in out.splitlines() if "sql instances create" in line)
    for flag in (
        "--tier=db-custom-1-3840",
        "--deletion-protection",
        "max_connections=100",
        "--retained-transaction-log-days=7",
        "--storage-size=30GB",
    ):
        assert flag in sql, flag
    # the prod worker waits for the AI package, so its OpenAI key is not asked for yet
    assert "OPENAI_API_KEY: " not in out


def test_initial_password_secrets_never_collide(toolkit: Path, tmp_path: Path) -> None:
    """Two addresses that fold to the same readable id still get two secrets."""
    names = []
    for email in ("A.B@x.test", "a_b@x.test"):
        out = _run(
            toolkit,
            tmp_path,
            "staging",
            "tenant",
            "set-password",
            "--email",
            email,
            "--staff-email",
            "s@a.test",
            "--dry-run",
            "--sha",
            SHA,
        ).stdout
        match = re.search(r"secrets create (INITIAL_PASSWORD_a_b_x_test_[0-9a-f]{12}) ", out)
        assert match is not None, out
        names.append(match.group(1))
    assert names[0] != names[1]


def test_sha_flag_needs_a_value(toolkit: Path, tmp_path: Path) -> None:
    result = _run(toolkit, tmp_path, "staging", "status", "--dry-run", "--sha")
    assert result.returncode == 2 and "unbound variable" not in result.stderr


def test_build_refuses_a_dirty_tree(toolkit: Path, tmp_path: Path) -> None:
    repo = toolkit.parents[2]
    git = [
        "git",
        "-C",
        str(repo),
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgsign=false",
    ]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "x"], check=True)
    clean = _run(toolkit, tmp_path, "staging", "build", "--dry-run")
    assert clean.returncode == 0, clean.stderr
    assert "builds submit" in clean.stdout and "--config=" in clean.stdout
    assert re.search(
        r"_IMAGE=asia-south1-docker\.pkg\.dev/aimos-staging/aimos/backend:sha-[0-9a-f]{12}",
        clean.stdout,
    )
    with (toolkit / "staging.env").open("a", encoding="utf-8") as env_file:
        env_file.write("# edited\n")
    dirty = _run(toolkit, tmp_path, "staging", "build", "--dry-run")
    assert dirty.returncode == 1
    assert "uncommitted changes" in dirty.stderr


# ---- what a deployed process receives passes the boot checks (app/core/config.py) ----


def _boot(monkeypatch: pytest.MonkeyPatch, env: dict[str, str], secrets: list[str]) -> None:
    from app.core.config import get_settings

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for name in secrets:
        monkeypatch.setenv(name, SECRET_VALUES[name])
    get_settings.cache_clear()
    get_settings()


@pytest.mark.parametrize("env_name", ["staging", "prod"])
def test_api_env_boots(
    toolkit: Path, tmp_path: Path, clean_env: Path, monkeypatch: pytest.MonkeyPatch, env_name: str
) -> None:
    out = _run(toolkit, tmp_path, env_name, "deploy", "--dry-run", "--sha", SHA).stdout
    env = _api_env(out)
    assert env["JWT_ISSUER"] == API_URL
    assert env["AMAZON_OAUTH_REDIRECT_URI"] == f"{API_URL}/api/v1/marketplaces/amazon/callback"
    assert (
        env["STORAGE_BACKEND"] == "gcs"
        and env["S3_ENDPOINT_URL"] == "https://storage.googleapis.com"
    )
    assert env["APP_ENV"] == ("staging" if env_name == "staging" else "production")
    deploy = next(line for line in out.splitlines() if line.startswith("+ gcloud run deploy "))
    for flag in ("--allow-unauthenticated", "--set-cloudsql-instances=", "--concurrency=40"):
        assert flag in deploy, flag
    secrets = _secret_names(deploy)
    assert "OPENAI_API_KEY" not in secrets
    _boot(monkeypatch, env, secrets)


def test_staging_worker_env_boots(
    toolkit: Path, tmp_path: Path, clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    spec: dict[str, Any] = _worker_spec(out)
    assert spec["kind"] == "WorkerPool"
    assert spec["metadata"]["annotations"]["run.googleapis.com/scalingMode"] == "manual"
    template = spec["spec"]["template"]
    conn = "aimos-staging:asia-south1:aimos-db"
    assert template["metadata"]["annotations"]["run.googleapis.com/cloudsql-instances"] == conn
    container = template["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in container["env"] if "value" in e}
    secrets = [e["name"] for e in container["env"] if "valueFrom" in e]
    assert env["APP_ROLE"] == "worker" and "OPENAI_API_KEY" in secrets
    assert "JWT_ISSUER" not in env
    _boot(monkeypatch, env, secrets)


def test_staging_worker_flags_path(toolkit: Path, tmp_path: Path) -> None:
    out = _run(
        toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA, worker_deploy=True
    ).stdout
    line = next(line for line in out.splitlines() if "run worker-pools deploy" in line)
    assert "--instances=1" in line and "--set-cloudsql-instances=" in line
    assert "OPENAI_API_KEY" in _secret_names(line)


def test_worker_gets_the_vision_provider_key(toolkit: Path, tmp_path: Path) -> None:
    """B2 (ai-deps C18): the worker boot refuses a missing vision key, so the toolkit asks for it
    and hands it to the worker pool, on both deploy paths."""
    from app.core.config import VISION_PROVIDER_KEY_ENV, Settings

    name = VISION_PROVIDER_KEY_ENV[Settings.model_fields["llm_provider_vision"].default]
    setup = _run(toolkit, tmp_path, "staging", "setup", "--dry-run", "--sha", SHA).stdout
    assert f'bash -c \'read -rsp "{name}: " v; echo; printf %s "$v" | ' in setup
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    spec: dict[str, Any] = _worker_spec(out)
    container = spec["spec"]["template"]["spec"]["containers"][0]
    assert name in [e["name"] for e in container["env"] if "valueFrom" in e]
    flags = _run(
        toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA, worker_deploy=True
    ).stdout
    line = next(line for line in flags.splitlines() if "run worker-pools deploy" in line)
    assert name in _secret_names(line)


def test_prod_worker_is_off_because_the_stub_refuses(
    toolkit: Path, tmp_path: Path, clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _run(toolkit, tmp_path, "prod", "deploy", "--dry-run", "--sha", SHA).stdout
    assert "worker pool skipped (WORKER_ENABLED=false in prod.env)" in out
    staging = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    spec: dict[str, Any] = _worker_spec(staging)
    container = spec["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in container["env"] if "value" in e}
    env["APP_ENV"] = "production"
    with pytest.raises(SystemExit, match="refuses mkos-pipeline-stub"):
        _boot(monkeypatch, env, [e["name"] for e in container["env"] if "valueFrom" in e])


def test_every_variable_is_in_the_register(toolkit: Path, tmp_path: Path, repo_root: Path) -> None:
    """Every env var the toolkit sets on a deployed process is a Settings field and an ENV.md row."""
    from app.core.config import Settings

    register = (repo_root / "docs/canon/ENV.md").read_text(encoding="utf-8")
    fields = {name.upper() for name in Settings.model_fields}
    names: set[str] = set()
    for env_name in ("staging", "prod"):
        out = _run(
            toolkit, tmp_path, env_name, "deploy", "--dry-run", "--sha", SHA, worker_deploy=True
        ).stdout
        for block in _blocks(out, "env file"):
            names.update(yaml.safe_load(block))
        for line in out.splitlines():
            if "--set-secrets=" in line:
                names.update(_secret_names(line))
    assert {"OPENAI_API_KEY", "JWT_ISSUER", "KMS_KEY_URI"} <= names
    for name in names:
        assert name in fields, name
        assert f"| `{name}` |" in register, name


def test_socket_database_url_is_accepted(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ENV.md DATABASE_URL in its Cloud Run form (the /cloudsql socket) passes the settings
    pattern, and the queue's libpq conninfo keeps the socket host."""
    import psycopg
    from app.core.config import get_settings
    from app.db.queue import libpq_conninfo

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("APP_ROLE", "shell")
    monkeypatch.setenv("DATABASE_URL", SECRET_VALUES["DATABASE_URL"])
    url = get_settings().database_url
    assert url is not None
    parts = psycopg.conninfo.conninfo_to_dict(libpq_conninfo(url))
    assert parts["host"] == "/cloudsql/aimos-staging:asia-south1:aimos-db"
    assert parts["dbname"] == "marketplace_os" and parts["user"] == "app_rw"


@pytest.mark.parametrize("env_name", ["staging", "prod"])
def test_env_files_hold_no_secret(repo_root: Path, env_name: str) -> None:
    text = (repo_root / DEPLOY / f"{env_name}.env").read_text(encoding="utf-8")
    keys = re.findall(r"^([A-Z_]+)=", text, re.MULTILINE)
    assert len(keys) == len(set(keys))
    for forbidden in (*SECRET_VALUES, "POSTGRES_PASSWORD", "APP_RW_PASSWORD", "MIGRATOR_PASSWORD"):
        assert forbidden not in keys, forbidden
    values = dict(re.findall(r"^([A-Z_]+)=(.*)$", text, re.MULTILINE))
    assert values["REGION"] == "asia-south1"
    assert values["KMS_KEYRING"] == "marketplace-os" and values["KMS_KEY"] == "oauth-token-key"
    assert values["FLIPKART_ENABLED"] == "false" and values["AMAZON_USE_SANDBOX"] == "true"
    # The placeholder until the dashboard is hosted, then the committed https origins (runbook).
    for name in ("ALLOWED_ORIGINS", "DASHBOARD_BASE_URL"):
        for origin in values[name].split(","):
            assert origin == "__FILL_ME__" or re.fullmatch(r"https://[a-z0-9.-]+", origin), name
    assert values["PROJECT"] == {"staging": "aimos-staging", "prod": "aimos-prod"}[env_name]


def test_runbook_covers_the_toolkit(repo_root: Path) -> None:
    text = (repo_root / "docs/runbooks/DEPLOY-GCP.md").read_text(encoding="utf-8")
    for name in SUBCOMMANDS:
        assert f"`{name}`" in text, name
    assert "gcloud run services update-traffic" in text and "--to-revisions" in text
    assert "point-in-time" in text.lower()
    assert "mkos-pipeline-stub" in text


# ---- scripts/db/run_bootstrap.py, offline ----


@pytest.fixture(scope="module")
def runner(script: Callable[[str], ModuleType]) -> ModuleType:
    return script("scripts/db/run_bootstrap.py")


def test_runner_reads_the_password_files_own_queries(runner: ModuleType) -> None:
    text = runner.PASSWORDS_SQL.read_text(encoding="utf-8")
    settable = runner.psql_query(text, "lmes_settable")
    assert settable.startswith("SELECT current_setting('log_min_error_statement') <> 'panic'")
    safe = runner.psql_query(text, "pw_safe")
    assert safe.startswith("SELECT current_setting('log_statement') = 'none'")
    assert safe.endswith("AS pw_safe") and "\\" not in safe
    for setting in ("'pg_stat_statements.track_utility'", "'pgaudit.log'", "MEMBER WITH ADMIN"):
        assert setting in safe, setting
    with pytest.raises(ValueError, match="no missing query"):
        runner.psql_query(text, "missing")


def test_runner_secret_files(runner: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "pw"
    path.write_text("hunter2-not-real\n", encoding="ascii")
    assert runner.read_secret(path) == "hunter2-not-real"
    verifier = runner.verifiers({"app_rw": path, "app_ro": None})
    assert list(verifier) == ["app_rw"]
    assert verifier["app_rw"].startswith("SCRAM-SHA-256$4096:")


def test_runner_bad_file_exits_2_without_connecting(
    runner: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "pw"
    bad.write_bytes("caf\xe9-secret".encode("latin-1"))
    args = ["--host", "/nowhere", "--dbname", "d", "--user", "u"]
    assert runner.main([*args, "--password-file", str(tmp_path / "absent")]) == 2
    assert runner.main([*args, "--password-file", str(bad)]) == 2
    err = capsys.readouterr().err
    assert "a password file is unreadable" in err and "secret" not in err


# ---- DPP-1 (ADR-0021): the MFA key, the MFA switch, generated passwords, unlock and reset-mfa ----


def _set_env(toolkit: Path, env_name: str, **values: str) -> None:
    path = toolkit / f"{env_name}.env"
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.MULTILINE)
    path.write_text(text, encoding="utf-8")


def _kms_bindings(out: str, key: str) -> list[str]:
    return [line for line in out.splitlines() if f"kms keys add-iam-policy-binding {key} " in line]


def test_mfa_key_created_with_rotation(toolkit: Path, tmp_path: Path) -> None:
    out = _run(toolkit, tmp_path, "staging", "setup", "--dry-run", "--sha", SHA).stdout
    create = next(
        line for line in out.splitlines() if "kms keys create mfa-secret-key " in line
    ).replace("\\", "")
    for flag in ("--rotation-period=90d", "--purpose=encryption", "--next-rotation-time=",
                 "--labels=app=aimos,env=staging", "--keyring=marketplace-os"):  # fmt: skip
        assert flag in create, flag


def test_only_api_sa_has_mfa_key_role(toolkit: Path, tmp_path: Path) -> None:
    out = _run(toolkit, tmp_path, "staging", "setup", "--dry-run", "--sha", SHA).stdout
    (binding,) = _kms_bindings(out, "mfa-secret-key")
    assert "aimos-api@" in binding
    assert "--role=roles/cloudkms.cryptoKeyEncrypterDecrypter " in binding


def test_worker_has_no_mfa_binding(toolkit: Path, tmp_path: Path) -> None:
    out = _run(toolkit, tmp_path, "staging", "setup", "--dry-run", "--sha", SHA).stdout
    for line in _kms_bindings(out, "mfa-secret-key"):
        for account in ("aimos-worker@", "aimos-ops@", "aimos-migrate@", "aimos-build@"):
            assert account not in line, account


def test_oauth_key_bindings_unchanged(toolkit: Path, tmp_path: Path) -> None:
    out = _run(toolkit, tmp_path, "staging", "setup", "--dry-run", "--sha", SHA).stdout
    bindings = _kms_bindings(out, "oauth-token-key")
    assert len(bindings) == 2
    (api,) = [line for line in bindings if "aimos-api@" in line]
    assert "--role=roles/cloudkms.cryptoKeyEncrypter " in api  # still encrypt only


def test_mfa_key_uri_only_in_api_env(toolkit: Path, tmp_path: Path) -> None:
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    env = _api_env(out)
    assert env["MFA_KMS_KEY_URI"] == (
        "projects/aimos-staging/locations/asia-south1/keyRings/marketplace-os/cryptoKeys/"
        "mfa-secret-key"
    )
    spec: dict[str, Any] = _worker_spec(out)
    container = spec["spec"]["template"]["spec"]["containers"][0]
    names = {e["name"] for e in container["env"]}
    assert not {"MFA_KMS_KEY_URI", "MFA_ENFORCEMENT", "MFA_GRACE_UNTIL"} & names
    tenant = _run(
        toolkit, tmp_path, "staging", "tenant", "unlock", "--email", "a@b.test",
        "--staff-email", "s@b.test", "--dry-run", "--sha", SHA,
    ).stdout  # fmt: skip
    assert "MFA_KMS_KEY_URI" not in tenant


@pytest.mark.parametrize("env_name", ["staging", "prod"])
def test_env_files_carry_mfa_keys(repo_root: Path, env_name: str) -> None:
    text = (repo_root / DEPLOY / f"{env_name}.env").read_text(encoding="utf-8")
    values = dict(re.findall(r"^([A-Z_]+)=(.*)$", text, re.MULTILINE))
    assert values["MFA_ENFORCEMENT"] == "off"
    assert values["MFA_GRACE_UNTIL"] == ""
    assert values["KMS_MFA_KEY"] == "mfa-secret-key"


def test_api_env_carries_mfa_enforcement_and_grace(toolkit: Path, tmp_path: Path) -> None:
    grace = "2026-11-20T00:00:00+05:30"
    _set_env(toolkit, "staging", MFA_ENFORCEMENT="all", MFA_GRACE_UNTIL=grace)
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    env = _api_env(out)
    assert env["MFA_ENFORCEMENT"] == "all" and env["MFA_GRACE_UNTIL"] == grace


@pytest.mark.parametrize("env_name", ["staging", "prod"])
def test_api_env_boots_under_all(
    toolkit: Path, tmp_path: Path, clean_env: Path, monkeypatch: pytest.MonkeyPatch, env_name: str
) -> None:
    _set_env(toolkit, env_name, MFA_ENFORCEMENT="all", MFA_GRACE_UNTIL="2026-11-20T00:00:00+05:30")
    out = _run(toolkit, tmp_path, env_name, "deploy", "--dry-run", "--sha", SHA).stdout
    env = _api_env(out)
    deploy = next(line for line in out.splitlines() if line.startswith("+ gcloud run deploy "))
    _boot(monkeypatch, env, _secret_names(deploy))


@pytest.mark.parametrize("command", ["release", "deploy", "tenant"])
def test_all_without_grace_refused(toolkit: Path, tmp_path: Path, command: str) -> None:
    _set_env(toolkit, "staging", MFA_ENFORCEMENT="all", MFA_GRACE_UNTIL="")
    result = _run(toolkit, tmp_path, "staging", command, "--dry-run", "--sha", SHA)
    assert result.returncode != 0
    assert "MFA_ENFORCEMENT=all needs MFA_GRACE_UNTIL" in result.stderr


@pytest.mark.parametrize(
    "grace", ["2026-11-20", "2026-11-20T00:00:00", "soon", "2026-13-01T00:00:00+05:30"]
)
def test_bad_grace_date_refused(toolkit: Path, tmp_path: Path, grace: str) -> None:
    _set_env(toolkit, "staging", MFA_ENFORCEMENT="all", MFA_GRACE_UNTIL=grace)
    for command in ("release", "deploy"):
        result = _run(toolkit, tmp_path, "staging", command, "--dry-run", "--sha", SHA)
        assert result.returncode != 0
        assert "MFA_GRACE_UNTIL must be ISO 8601 with an offset" in result.stderr


def test_bad_enforcement_value_refused(toolkit: Path, tmp_path: Path) -> None:
    _set_env(toolkit, "staging", MFA_ENFORCEMENT="admins")
    result = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA)
    assert result.returncode != 0 and "MFA_ENFORCEMENT must be off or all" in result.stderr


def _gen_passwords(toolkit: Path, n: int) -> list[str]:
    script = toolkit / "deploy.sh"
    source = script.read_text(encoding="utf-8")
    start = source.index("gen_password() {")
    body = source[start : source.index("\n}\n", start) + 3]
    loop = f'for _ in $(seq {n}); do gen_password; printf "\\n"; done'
    result = subprocess.run(
        ["bash", "-c", body + "\n" + loop], capture_output=True, text=True, timeout=120, check=True
    )
    return result.stdout.splitlines()


def test_generated_password_passes_policy(toolkit: Path) -> None:
    from app.core.password_policy import check

    passwords = _gen_passwords(toolkit, 500)
    assert len(passwords) == 500 and len(set(passwords)) == 500
    samples = [("asha.rao@x.in", "Asha Rao"), ("ab@x.in", None), ("owner@acme.test", "Owner")]
    for password in passwords:
        assert len(password) == 31 and re.fullmatch(
            r"([A-Z][a-z][0-9]-){7}[A-Z][a-z][0-9]", password
        )
        for email, name in samples:
            assert check(password, email=email, display_name=name) == [], (password, email)


def test_ensure_password_no_longer_uses_hex(toolkit: Path, tmp_path: Path) -> None:
    out = _run(
        toolkit, tmp_path, "staging", "tenant", "set-password", "--email", "a@b.test",
        "--staff-email", "s@b.test", "--dry-run", "--sha", SHA,
    ).stdout  # fmt: skip
    note = next(
        line for line in out.splitlines() if "secrets versions add INITIAL_PASSWORD_" in line
    )
    assert note.startswith("+ gen_password | gcloud secrets versions add")
    assert "openssl rand -hex 16" not in out


def test_tenant_unlock_has_no_password_mount(toolkit: Path, tmp_path: Path) -> None:
    out = _run(
        toolkit, tmp_path, "staging", "tenant", "unlock", "--email", "a@b.test",
        "--staff-email", "s@b.test", "--dry-run", "--sha", SHA,
    ).stdout  # fmt: skip
    job = next(line for line in out.splitlines() if "run jobs deploy aimos-ops" in line)
    assert "/secrets/initial-password/value" not in job
    assert _secret_names(job) == ["ALEMBIC_DATABASE_URL"]
    args = job.replace("\\", "")  # the printed command is shell-quoted (printf %q)
    assert "--args=^|^-m|app.cli|user|unlock|--email=a@b.test|--staff-email=s@b.test" in args
    assert "INITIAL_PASSWORD_" not in out


def test_tenant_reset_mfa_mounts_fresh_password(toolkit: Path, tmp_path: Path) -> None:
    out = _run(
        toolkit, tmp_path, "staging", "tenant", "reset-mfa", "--email", "a@b.test",
        "--staff-email", "s@b.test", "--dry-run", "--sha", SHA,
    ).stdout  # fmt: skip
    assert "+ gen_password | gcloud secrets versions add INITIAL_PASSWORD_" in out
    job = next(line for line in out.splitlines() if "run jobs deploy aimos-ops" in line)
    assert "/secrets/initial-password/value=INITIAL_PASSWORD_" in job
    assert "|user|reset-mfa|--email=a@b.test|--staff-email=s@b.test" in job.replace("\\", "")
