"""The by-hand GCP deploy toolkit (scripts/deploy/gcp/, docs/runbooks/DEPLOY-GCP.md) and its
Cloud SQL bootstrap runner (scripts/db/run_bootstrap.py). Nothing here contacts Google Cloud: the
toolkit runs under --dry-run with a fake `gcloud` that records every call but a local `--help`."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections.abc import Callable, Iterator
from datetime import datetime
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
    "templates",
    "status",
    "edge",
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
    "DEEPSEEK_API_KEY": "sk-ds-test-" + "9" * 32,
    "OPENROUTER_API_KEY": "sk-or-test-" + "8" * 32,
    "GOOGLE_OAUTH_CLIENT_SECRET": "test-only-google-client-secret-" + "f" * 8,
    "AMAZON_CLIENT_SECRET": "test-only-amazon-client-secret-" + "7" * 8,
    # ADR-0025: prod.env runs EDGE_PROVIDER=cloudflare, which binds the Worker's origin secret
    "EDGE_ORIGIN_SECRET": "6" * 64,
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
        if not source.is_file():
            continue
        text = source.read_text(encoding="utf-8")
        if source.suffix == ".env":
            text = text.replace("__FILL_ME__", ORIGIN)
            # prod.env pins the cutover values (EDGE-CUTOVER step 6); the copy uses the test URL
            # and an unpinned issuer, as the tests expect.
            text = re.sub(r"^API_PUBLIC_URL=.*$", f"API_PUBLIC_URL={API_URL}", text, flags=re.M)
            text = re.sub(r"^JWT_ISSUER=.*$", "JWT_ISSUER=", text, flags=re.M)
            # Google sign-in starts off in the copy; the Google tests switch it on themselves.
            text = re.sub(
                r"^GOOGLE_OAUTH_ENABLED=.*$", "GOOGLE_OAUTH_ENABLED=false", text, flags=re.M
            )
            # The Amazon app starts off in the copy too; the Amazon tests set AMAZON_APP_ID.
            text = re.sub(r"^AMAZON_APP_ID=.*$", "AMAZON_APP_ID=", text, flags=re.M)
            text = re.sub(r"^AMAZON_CLIENT_ID=.*$", "AMAZON_CLIENT_ID=", text, flags=re.M)
            text = re.sub(r"^AMAZON_USE_SANDBOX=.*$", "AMAZON_USE_SANDBOX=true", text, flags=re.M)
            # prod.env enforces the origin guard; the copy starts off, as the tests expect.
            text = re.sub(r"^ORIGIN_AUTH=.*$", "ORIGIN_AUTH=off", text, flags=re.M)
            # prod.env pins the real pipeline with the worker on; the copy keeps the stub baseline
            # and the pipeline tests set PIPELINE_REF themselves.
            if source.name == "prod.env":
                text = re.sub(r"^PIPELINE_REF=.*$", "PIPELINE_REF=", text, flags=re.M)
                text = re.sub(r"^WORKER_ENABLED=.*$", "WORKER_ENABLED=false", text, flags=re.M)
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
    # the prod worker waits for the AI package, so its model keys are not asked for yet
    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"):
        assert f"{name}: " not in out, name


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
    # prod left the sandbox once the Amazon app cleared (2026-11-05); staging stays on it.
    sandbox = {"staging": "true", "prod": "false"}[env_name]
    assert values["FLIPKART_ENABLED"] == "false" and values["AMAZON_USE_SANDBOX"] == sandbox
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
    assert values["KMS_MFA_KEY"] == "mfa-secret-key"
    if env_name == "prod":
        # MFA-STAFF §3 step 5: prod enforces, with a grace date in ISO 8601 with an offset
        assert values["MFA_ENFORCEMENT"] == "all"
        grace = datetime.fromisoformat(values["MFA_GRACE_UNTIL"])
        assert grace.utcoffset() is not None
    else:
        assert values["MFA_ENFORCEMENT"] == "off"
        assert values["MFA_GRACE_UNTIL"] == ""


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


def _gen_passwords(toolkit: Path, n: int, email: str = "owner@acme.test") -> list[str]:
    script = toolkit / "deploy.sh"
    source = script.read_text(encoding="utf-8")
    start = source.index("gen_password() {")
    body = source[start : source.index("\n}\n", start) + 3]
    loop = f'for _ in $(seq {n}); do gen_password "{email}"; printf "\\n"; done'
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


def test_generated_password_avoids_the_email_local_part(toolkit: Path) -> None:
    """Review E2E-3: the whole local part (digits and '-' included) is a personal token. "1-a"
    sits in about 2.7% of raw values (500 samples: ~13 hits), so a generator that ignores the
    email fails here; gen_password draws again on a hit."""
    from app.core.password_policy import check

    for email in ("1-a@x.in", "ab1@x.in"):
        for password in _gen_passwords(toolkit, 500, email):
            assert check(password, email=email, display_name=None) == [], (password, email)


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


# ---- Amazon DPP: security logs, backup location, the KMS guard ----

# A gcloud that answers like a project whose MFA key was never created: no live service, the OAuth
# key present, the MFA key absent. Any other call is recorded and fails.
FAKE_GCLOUD_NO_MFA_KEY = """#!/bin/sh
case " $* " in
  *" --help "*) exit 0 ;;
  *"run services describe"*) echo "ERROR: NOT_FOUND" >&2; exit 1 ;;
  *"kms keys describe oauth-token-key "*) echo "projects/p/cryptoKeys/oauth-token-key"; exit 0 ;;
  *"kms keys describe mfa-secret-key "*) echo "ERROR: NOT_FOUND" >&2; exit 1 ;;
esac
echo "$*" >> "$FAKE_GCLOUD_LOG"
exit 98
"""


def test_setup_keeps_api_security_events_400_days(toolkit: Path, tmp_path: Path) -> None:
    """DPP 2.6.1: the api's sign-in events and refused requests go to the 400-day bucket."""
    out = _run(toolkit, tmp_path, "prod", "setup", "--dry-run", "--sha", SHA).stdout
    lines = out.splitlines()
    bucket = next(x for x in lines if "logging buckets create security-logs" in x)
    assert "--retention-days=400" in bucket and "--location=global" in bucket
    sink = shlex.split(next(x for x in lines if "logging sinks create app-security" in x))
    assert (
        "logging.googleapis.com/projects/aimos-prod/locations/global/buckets/security-logs" in sink
    )
    (log_filter,) = [a.split("=", 1)[1] for a in sink if a.startswith("--log-filter=")]
    assert 'resource.labels.service_name="aimos-api"' in log_filter
    assert 'jsonPayload.message=~"^auth\\\\."' in log_filter
    for status in ("401", "403", "429"):
        assert f"httpRequest.status={status}" in log_filter, status
    assert 'NOT httpRequest.requestUrl:"/callback"' in log_filter
    # the bucket comes before the sink that writes into it
    assert lines.index(bucket) < next(i for i, x in enumerate(lines) if "app-security" in x)


def test_backup_location_follows_the_env_file(toolkit: Path, tmp_path: Path) -> None:
    """DPP 2.7.4: SQL_BACKUP_LOCATION moves the backups; staging (no key) keeps its region."""

    def create_line(env_name: str) -> str:
        out = _run(toolkit, tmp_path, env_name, "setup", "--dry-run", "--sha", SHA).stdout
        return next(line for line in out.splitlines() if "sql instances create" in line)

    assert "--backup-location=asia-south1" in create_line("prod")
    assert "--backup-location=asia-south1" in create_line("staging")
    env = toolkit / "prod.env"
    text = env.read_text(encoding="utf-8")
    assert "SQL_BACKUP_LOCATION=asia-south1\n" in text
    env.write_text(
        text.replace("SQL_BACKUP_LOCATION=asia-south1\n", "SQL_BACKUP_LOCATION=asia-south2\n"),
        encoding="utf-8",
    )
    assert "--backup-location=asia-south2" in create_line("prod")


def test_existing_instance_backup_location_is_patched(repo_root: Path) -> None:
    """The live instance path compares the backups' location and patches only that flag."""
    script = (repo_root / DEPLOY / "deploy.sh").read_text(encoding="utf-8")
    body = script[
        script.index("setup_sql() {") : script.index("wait_sql_runnable\n  gq sql databases")
    ]
    assert "settings.backupConfiguration.location" in body
    assert 'gc sql instances patch "$SQL_INSTANCE" --backup-location="$SQL_BACKUP_LOCATION"' in body


def test_dry_run_deploy_reports_the_kms_guard(toolkit: Path, tmp_path: Path) -> None:
    out = _run(toolkit, tmp_path, "prod", "deploy", "--dry-run", "--sha", SHA).stdout
    lines = out.splitlines()
    guard = lines.index("deploy: kms guard: skipped (dry run)")
    assert guard < next(i for i, x in enumerate(lines) if "gcloud run deploy aimos-api" in x)


def test_release_runs_the_kms_guard_before_building(repo_root: Path) -> None:
    script = (repo_root / DEPLOY / "deploy.sh").read_text(encoding="utf-8")
    release = script[script.index("cmd_release() {") :]
    release = release[: release.index("\n}\n")]
    assert release.index("kms_guard") < release.index("cmd_build")


def test_deploy_refuses_when_the_mfa_key_is_missing(toolkit: Path, tmp_path: Path) -> None:
    """A release that skipped `setup` stops before `run deploy`, naming the key and the fix."""
    bin_dir = tmp_path / "bin-no-mfa"
    bin_dir.mkdir()
    fake = bin_dir / "gcloud"
    fake.write_text(FAKE_GCLOUD_NO_MFA_KEY, encoding="utf-8")
    fake.chmod(0o755)
    log = tmp_path / "gcloud-no-mfa.log"
    result = subprocess.run(
        ["bash", str(toolkit / "deploy.sh"), "prod", "deploy", "--sha", SHA],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "FAKE_GCLOUD_LOG": str(log),
        },
        cwd=tmp_path,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "KMS key mfa-secret-key is missing or unreadable in aimos-prod" in result.stderr
    assert "deploy.sh prod setup first" in result.stderr
    assert not log.exists(), log.read_text(encoding="utf-8")


# ---- DPP 1.7: the daily maintenance job and its scheduler ----

MAINT_SECRET_NAMES = {
    "DATABASE_URL",
    "ALEMBIC_DATABASE_URL",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "SENTRY_DSN",
}


def _secret_grants(out: str, account: str) -> set[str]:
    grants = set()
    for line in out.splitlines():
        if "secrets add-iam-policy-binding" in line and f"serviceAccount:{account}@" in line:
            grants.add(shlex.split(line)[4])
    return grants


@pytest.mark.parametrize("env_name", ["staging", "prod"])
def test_setup_prepares_the_maintenance_job(toolkit: Path, tmp_path: Path, env_name: str) -> None:
    """`setup` enables Cloud Scheduler and creates the two identities: the job's (Cloud SQL client
    and its four secrets only) and the scheduler's (no project role, no secret)."""
    out = _run(toolkit, tmp_path, env_name, "setup", "--dry-run", "--sha", SHA).stdout
    lines = out.splitlines()
    enable = next(x for x in lines if "gcloud services enable" in x)
    assert "cloudscheduler.googleapis.com" in enable
    for name in ("aimos-maint", "aimos-sched"):
        assert any(f"iam service-accounts create {name} " in x for x in lines), name
    project_roles = [x for x in lines if "projects add-iam-policy-binding" in x]
    maint_roles = [x for x in project_roles if "serviceAccount:aimos-maint@" in x]
    assert len(maint_roles) == 1 and "--role=roles/cloudsql.client" in maint_roles[0]
    assert not [x for x in project_roles if "serviceAccount:aimos-sched@" in x]
    assert _secret_grants(out, "aimos-maint") == MAINT_SECRET_NAMES
    assert _secret_grants(out, "aimos-sched") == set()


def test_deploy_ships_the_maintenance_job_and_its_trigger(toolkit: Path, tmp_path: Path) -> None:
    out = _run(toolkit, tmp_path, "prod", "deploy", "--dry-run", "--sha", SHA).stdout
    lines = out.splitlines()
    job_at = next(i for i, x in enumerate(lines) if "run jobs deploy aimos-maintenance " in x)
    job = shlex.split(lines[job_at])
    assert "--service-account=aimos-maint@aimos-prod.iam.gserviceaccount.com" in job
    assert "--args=-m,app.workers.maintenance" in job
    assert "--set-cloudsql-instances=aimos-prod:asia-south1:aimos-db" in job
    assert "--tasks=1" in job and "--parallelism=1" in job
    (secrets,) = [a for a in job if a.startswith("--set-secrets=")]
    assert set(_secret_names(secrets)) == MAINT_SECRET_NAMES
    env = yaml.safe_load(_blocks(out, "env file")[-1])
    assert env["APP_ROLE"] == "shell"  # no worker boot checks: the job needs no pipeline
    assert "MFA_KMS_KEY_URI" not in env and "JWT_ISSUER" not in env
    invoker_at = next(
        i for i, x in enumerate(lines) if "run jobs add-iam-policy-binding aimos-maintenance " in x
    )
    invoker = shlex.split(lines[invoker_at])
    assert "--member=serviceAccount:aimos-sched@aimos-prod.iam.gserviceaccount.com" in invoker
    assert "--role=roles/run.invoker" in invoker
    trigger_at = next(
        i for i, x in enumerate(lines) if "scheduler jobs create http aimos-maintenance-daily" in x
    )
    trigger = shlex.split(lines[trigger_at])
    assert "--schedule=0 3 * * *" in trigger and "--time-zone=Asia/Kolkata" in trigger
    assert (
        "--uri=https://run.googleapis.com/v2/projects/aimos-prod/locations/asia-south1/jobs/"
        "aimos-maintenance:run"
    ) in trigger
    assert "--http-method=POST" in trigger
    assert "--oauth-service-account-email=aimos-sched@aimos-prod.iam.gserviceaccount.com" in trigger
    assert job_at < invoker_at < trigger_at


def test_maintenance_job_runs_a_real_module(repo_root: Path) -> None:
    """The job's `-m` target exists and runs every retention step plus the account purge."""
    module = repo_root / "apps/api/app/workers/maintenance.py"
    text = module.read_text(encoding="utf-8")
    for step in (
        "prune_oauth_states",
        "prune_idempotency_keys",
        "retention_daily",
        "purge_disconnected_accounts",
    ):
        assert f"periodic.{step}" in text, step


@pytest.mark.parametrize("env_name", ["staging", "prod"])
def test_env_files_carry_the_maintenance_keys(repo_root: Path, env_name: str) -> None:
    text = (repo_root / DEPLOY / f"{env_name}.env").read_text(encoding="utf-8")
    values = dict(re.findall(r"^([A-Z_]+)=(.*)$", text, re.MULTILINE))
    assert values["MAINT_JOB"] == "aimos-maintenance"
    assert values["SA_MAINT"] == "aimos-maint" and values["SA_SCHED"] == "aimos-sched"
    assert values["MAINT_SCHEDULE"] == '"0 3 * * *"'


# ---- the setup guard: a release refuses before the build when `setup` predates the job ----

FAKE_GCLOUD_OLD_SETUP = """#!/bin/sh
case " $* " in
  *" --help "*) exit 0 ;;
  *"run services describe"*) echo "ERROR: NOT_FOUND" >&2; exit 1 ;;
  *"kms keys describe"*) echo "projects/p/cryptoKeys/k"; exit 0 ;;
  *"iam service-accounts describe aimos-maint@"*) echo "aimos-maint@p"; exit 0 ;;
  *"iam service-accounts describe aimos-sched@"*) [ -n "$HAS_SCHED" ] && { echo "aimos-sched@p"; exit 0; }
    echo "ERROR: NOT_FOUND" >&2; exit 1 ;;
  *"services list"*) [ -n "$HAS_SCHEDULER_API" ] && echo "cloudscheduler.googleapis.com"; exit 0 ;;
esac
echo "$*" >> "$FAKE_GCLOUD_LOG"
exit 98
"""


def _run_old_setup(
    toolkit: Path, tmp_path: Path, *, sched: bool, api: bool
) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir = tmp_path / f"bin-old-{sched}-{api}"
    bin_dir.mkdir()
    fake = bin_dir / "gcloud"
    fake.write_text(FAKE_GCLOUD_OLD_SETUP, encoding="utf-8")
    fake.chmod(0o755)
    log = tmp_path / f"gcloud-old-{sched}-{api}.log"
    result = subprocess.run(
        ["bash", str(toolkit / "deploy.sh"), "prod", "deploy", "--sha", SHA],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "FAKE_GCLOUD_LOG": str(log),
            "HAS_SCHED": "1" if sched else "",
            "HAS_SCHEDULER_API": "1" if api else "",
        },
        cwd=tmp_path,
    )
    return result, log


def test_deploy_refuses_without_the_maintenance_accounts(toolkit: Path, tmp_path: Path) -> None:
    result, log = _run_old_setup(toolkit, tmp_path, sched=False, api=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "service account aimos-sched is missing or unreadable in aimos-prod" in result.stderr
    assert "deploy.sh prod setup first" in result.stderr
    assert not log.exists(), log.read_text(encoding="utf-8")  # nothing deployed


def test_deploy_refuses_without_cloud_scheduler(toolkit: Path, tmp_path: Path) -> None:
    result, log = _run_old_setup(toolkit, tmp_path, sched=True, api=False)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "cloudscheduler.googleapis.com is not enabled in aimos-prod" in result.stderr
    assert "deploy.sh prod setup first" in result.stderr
    assert not log.exists(), log.read_text(encoding="utf-8")


def test_dry_run_deploy_reports_the_setup_guard(toolkit: Path, tmp_path: Path) -> None:
    out = _run(toolkit, tmp_path, "prod", "deploy", "--dry-run", "--sha", SHA).stdout
    lines = out.splitlines()
    guard = lines.index("deploy: setup guard: skipped (dry run)")
    assert lines.index("deploy: kms guard: skipped (dry run)") < guard
    assert guard < next(i for i, x in enumerate(lines) if "gcloud run deploy aimos-api" in x)


def test_release_runs_the_setup_guard_before_building(repo_root: Path) -> None:
    script = (repo_root / DEPLOY / "deploy.sh").read_text(encoding="utf-8")
    release = script[script.index("cmd_release() {") :]
    release = release[: release.index("\n}\n")]
    assert release.index("kms_guard") < release.index("setup_guard") < release.index("cmd_build")


def test_runbook_names_the_setup_guard(repo_root: Path) -> None:
    text = (repo_root / "docs/runbooks/DEPLOY-GCP.md").read_text(encoding="utf-8")
    assert "setup guard" in text and "aimos-maint" in text and "run setup first" in text


# ---- ADR-0020: the Google sign-in switch ----

GOOGLE_CLIENT = "1039991710726-74ijetqns5m41m2m3c6piso02j3vub2p.apps.googleusercontent.com"


@pytest.mark.parametrize("env_name", ["staging", "prod"])
def test_env_files_ship_the_google_switch(repo_root: Path, env_name: str) -> None:
    text = (repo_root / DEPLOY / f"{env_name}.env").read_text(encoding="utf-8")
    values = dict(re.findall(r"^([A-Z_]+)=(.*)$", text, re.MULTILINE))
    # prod runs Sign in with Google; staging has no Google client yet
    assert values["GOOGLE_OAUTH_ENABLED"] == ("true" if env_name == "prod" else "false")
    if env_name == "prod":
        assert values["GOOGLE_OAUTH_CLIENT_ID"] == GOOGLE_CLIENT
    assert "GOOGLE_OAUTH_CLIENT_SECRET" not in values


@pytest.mark.parametrize("env_name", ["staging", "prod"])
def test_google_off_binds_no_secret(
    toolkit: Path, tmp_path: Path, clean_env: Path, monkeypatch: pytest.MonkeyPatch, env_name: str
) -> None:
    out = _run(toolkit, tmp_path, env_name, "deploy", "--dry-run", "--sha", SHA).stdout
    env = _api_env(out)
    assert env["GOOGLE_OAUTH_ENABLED"] == "false"
    assert "deploy: google guard: sign-in with Google is off" in out
    deploy = next(line for line in out.splitlines() if line.startswith("+ gcloud run deploy "))
    secrets = _secret_names(deploy)
    assert "GOOGLE_OAUTH_CLIENT_SECRET" not in secrets
    _boot(monkeypatch, env, secrets)
    setup = _run(toolkit, tmp_path, env_name, "setup", "--dry-run", "--sha", SHA).stdout
    assert "GOOGLE_OAUTH_CLIENT_SECRET" not in _secret_grants(setup, "aimos-api")
    assert 'read -rsp "GOOGLE_OAUTH_CLIENT_SECRET: "' not in setup


def test_google_on_binds_the_secret_and_boots(
    toolkit: Path, tmp_path: Path, clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_env(toolkit, "prod", GOOGLE_OAUTH_ENABLED="true")
    out = _run(toolkit, tmp_path, "prod", "deploy", "--dry-run", "--sha", SHA).stdout
    env = _api_env(out)
    assert env["GOOGLE_OAUTH_ENABLED"] == "true"
    assert env["GOOGLE_OAUTH_CLIENT_ID"] == GOOGLE_CLIENT
    assert "deploy: google guard: skipped (dry run)" in out
    deploy = next(line for line in out.splitlines() if line.startswith("+ gcloud run deploy "))
    secrets = _secret_names(deploy)
    assert "GOOGLE_OAUTH_CLIENT_SECRET" in secrets
    _boot(monkeypatch, env, secrets)
    from app.core.config import get_settings

    # derived from DASHBOARD_BASE_URL, so the callback host always matches the dashboard
    assert get_settings().google_redirect_uri == (
        f"{env['DASHBOARD_BASE_URL']}/api/v1/auth/google/callback"
    )
    setup = _run(toolkit, tmp_path, "prod", "setup", "--dry-run", "--sha", SHA).stdout
    assert "GOOGLE_OAUTH_CLIENT_SECRET" in _secret_grants(setup, "aimos-api")
    assert 'bash -c \'read -rsp "GOOGLE_OAUTH_CLIENT_SECRET: " v; echo; printf %s "$v" | ' in setup
    for account in ("aimos-worker", "aimos-ops", "aimos-maint", "aimos-migrate"):
        assert "GOOGLE_OAUTH_CLIENT_SECRET" not in _secret_grants(setup, account), account


def test_google_worker_and_jobs_never_get_the_switch(toolkit: Path, tmp_path: Path) -> None:
    _set_env(toolkit, "staging", GOOGLE_OAUTH_ENABLED="true", GOOGLE_OAUTH_CLIENT_ID=GOOGLE_CLIENT)
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    for block in _blocks(out, "env file")[1:]:
        assert "GOOGLE_OAUTH" not in block
    spec: dict[str, Any] = _worker_spec(out)
    container = spec["spec"]["template"]["spec"]["containers"][0]
    assert not [e for e in container["env"] if e["name"].startswith("GOOGLE_")]


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"GOOGLE_OAUTH_ENABLED": "yes"}, "GOOGLE_OAUTH_ENABLED must be true or false"),
        (
            {"GOOGLE_OAUTH_ENABLED": "true", "GOOGLE_OAUTH_CLIENT_ID": ""},
            "GOOGLE_OAUTH_ENABLED=true needs GOOGLE_OAUTH_CLIENT_ID",
        ),
        (
            {"GOOGLE_OAUTH_ENABLED": "true", "GOOGLE_OAUTH_CLIENT_ID": "not-a-google-client"},
            "GOOGLE_OAUTH_ENABLED=true needs GOOGLE_OAUTH_CLIENT_ID",
        ),
    ],
)
def test_bad_google_switch_refused(
    toolkit: Path, tmp_path: Path, values: dict[str, str], message: str
) -> None:
    _set_env(toolkit, "staging", **values)
    for command in ("release", "deploy", "setup"):
        result = _run(toolkit, tmp_path, "staging", command, "--dry-run", "--sha", SHA)
        assert result.returncode != 0, command
        assert message in result.stderr, command


def test_release_runs_the_google_guard_before_building(repo_root: Path) -> None:
    script = (repo_root / DEPLOY / "deploy.sh").read_text(encoding="utf-8")
    for name in ("cmd_release() {", "cmd_deploy() {"):
        body = script[script.index(name) :]
        body = body[: body.index("\n}\n")]
        assert "google_guard" in body, name
    release = script[script.index("cmd_release() {") :]
    release = release[: release.index("\n}\n")]
    assert release.index("google_guard") < release.index("cmd_build")


# ---- the Amazon SP-API app: AMAZON_APP_ID is the switch ----

AMAZON_APP = "amzn1.sp.solution.0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
AMAZON_CLIENT = "amzn1.application-oa2-client.0123456789abcdef0123456789abcdef"
AMAZON_CONSENT = "https://sellercentral.amazon.in/apps/authorize/consent?version=beta"
AMAZON_KEYS = ("AMAZON_APP_ID", "AMAZON_CLIENT_ID", "AMAZON_AUTHORIZE_URL")


def _amazon_on(toolkit: Path, env_name: str) -> None:
    _set_env(toolkit, env_name, AMAZON_APP_ID=AMAZON_APP, AMAZON_CLIENT_ID=AMAZON_CLIENT)


def test_staging_env_ships_the_amazon_app_off(repo_root: Path) -> None:
    text = (repo_root / DEPLOY / "staging.env").read_text(encoding="utf-8")
    values = dict(re.findall(r"^([A-Z_]+)=(.*)$", text, re.MULTILINE))
    assert values["AMAZON_APP_ID"] == "" and values["AMAZON_CLIENT_ID"] == ""
    # a draft app consents only with version=beta (docs/EXTERNAL-SETUP.md)
    assert values["AMAZON_AUTHORIZE_URL"] == AMAZON_CONSENT
    assert values["AMAZON_USE_SANDBOX"] == "true"
    assert "AMAZON_CLIENT_SECRET" not in values


def test_prod_env_ships_the_registered_amazon_app(repo_root: Path) -> None:
    """The app cleared on 2026-11-05: prod names it, keeps the beta consent while it is a draft,
    talks to the production SP-API host, and never carries the client secret."""
    text = (repo_root / DEPLOY / "prod.env").read_text(encoding="utf-8")
    values = dict(re.findall(r"^([A-Z_]+)=(.*)$", text, re.MULTILINE))
    assert re.fullmatch(r"amzn1\.sp\.solution\.[0-9a-f-]{36}", values["AMAZON_APP_ID"])
    assert re.fullmatch(r"amzn1\.application-oa2-client\.[0-9a-f]{32}", values["AMAZON_CLIENT_ID"])
    assert values["AMAZON_AUTHORIZE_URL"] == AMAZON_CONSENT
    assert values["AMAZON_USE_SANDBOX"] == "false"
    assert "AMAZON_CLIENT_SECRET" not in values
    assert "amzn1.oa2-cs" not in text


@pytest.mark.parametrize("env_name", ["staging", "prod"])
def test_amazon_off_binds_no_secret(
    toolkit: Path, tmp_path: Path, clean_env: Path, monkeypatch: pytest.MonkeyPatch, env_name: str
) -> None:
    out = _run(toolkit, tmp_path, env_name, "deploy", "--dry-run", "--sha", SHA).stdout
    assert "deploy: amazon guard: the Amazon app is off (AMAZON_APP_ID blank)" in out
    env = _api_env(out)
    for key in AMAZON_KEYS:
        assert key not in env, key
    deploy = next(line for line in out.splitlines() if line.startswith("+ gcloud run deploy "))
    secrets = _secret_names(deploy)
    assert "AMAZON_CLIENT_SECRET" not in secrets
    _boot(monkeypatch, env, secrets)
    from app.adapters.oauth import get_oauth_client
    from app.core.config import get_settings

    # connect answers 501 without a client
    assert get_oauth_client("amazon", get_settings()) is None
    if env_name == "staging":
        worker_env, worker_secrets = _worker_container(out)
        assert "AMAZON_CLIENT_SECRET" not in worker_secrets
        assert not [k for k in worker_env if k in AMAZON_KEYS]
    setup = _run(toolkit, tmp_path, env_name, "setup", "--dry-run", "--sha", SHA).stdout
    # created empty, like the Google secret, but granted to no one and never asked for
    assert "gcloud secrets create AMAZON_CLIENT_SECRET " in setup
    for account in ("aimos-api", "aimos-worker"):
        assert "AMAZON_CLIENT_SECRET" not in _secret_grants(setup, account), account
    assert 'read -rsp "AMAZON_CLIENT_SECRET: "' not in setup


def test_amazon_on_binds_the_secret_to_api_and_worker_and_both_boot(
    toolkit: Path, tmp_path: Path, clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _amazon_on(toolkit, "staging")
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    assert "deploy: amazon guard: skipped (dry run)" in out
    env = _api_env(out)
    assert env["AMAZON_APP_ID"] == AMAZON_APP
    assert env["AMAZON_CLIENT_ID"] == AMAZON_CLIENT
    assert env["AMAZON_AUTHORIZE_URL"] == AMAZON_CONSENT
    deploy = next(line for line in out.splitlines() if line.startswith("+ gcloud run deploy "))
    secrets = _secret_names(deploy)
    assert "AMAZON_CLIENT_SECRET" in secrets
    _boot(monkeypatch, env, secrets)
    from app.adapters.oauth import get_oauth_client
    from app.core.config import get_settings

    client = get_oauth_client("amazon", get_settings())
    assert client is not None
    url = client.authorize_url(state="s", code_challenge=None)
    assert url.startswith("https://sellercentral.amazon.in/apps/authorize/consent?")
    assert "version=beta" in url and f"application_id={AMAZON_APP}" in url
    worker_env, worker_secrets = _worker_container(out)
    assert "AMAZON_CLIENT_SECRET" in worker_secrets
    for key in AMAZON_KEYS:
        assert worker_env[key] == env[key], key
    for key in (*env, *secrets):  # the worker boots on its own env, not the api's
        monkeypatch.delenv(key, raising=False)
    _boot(monkeypatch, worker_env, worker_secrets)
    assert get_oauth_client("amazon", get_settings()) is not None
    flags = _run(
        toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA, worker_deploy=True
    ).stdout
    line = next(line for line in flags.splitlines() if "run worker-pools deploy" in line)
    assert "AMAZON_CLIENT_SECRET" in _secret_names(line)
    setup = _run(toolkit, tmp_path, "staging", "setup", "--dry-run", "--sha", SHA).stdout
    for account in ("aimos-api", "aimos-worker"):
        assert "AMAZON_CLIENT_SECRET" in _secret_grants(setup, account), account
    assert 'bash -c \'read -rsp "AMAZON_CLIENT_SECRET: " v; echo; printf %s "$v" | ' in setup


@pytest.mark.parametrize("env_name", ["staging", "prod"])
def test_amazon_jobs_never_get_the_app(toolkit: Path, tmp_path: Path, env_name: str) -> None:
    _amazon_on(toolkit, env_name)
    out = _run(toolkit, tmp_path, env_name, "deploy", "--dry-run", "--sha", SHA).stdout
    blocks = _blocks(out, "env file")
    assert "AMAZON_APP_ID" in blocks[0]
    # every env file after the api's belongs to a job (the worker uses its spec here)
    for block in blocks[1:]:
        for key in AMAZON_KEYS:
            assert key not in block, key
    jobs = [x for x in out.splitlines() if "run jobs deploy" in x and "--set-secrets=" in x]
    assert jobs
    for line in jobs:
        assert "AMAZON_CLIENT_SECRET" not in _secret_names(line), line
    setup = _run(toolkit, tmp_path, env_name, "setup", "--dry-run", "--sha", SHA).stdout
    for account in ("aimos-ops", "aimos-maint", "aimos-migrate", "aimos-build"):
        assert "AMAZON_CLIENT_SECRET" not in _secret_grants(setup, account), account


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (
            {"AMAZON_APP_ID": "amzn1.sp.solution.REPLACE_ME", "AMAZON_CLIENT_ID": AMAZON_CLIENT},
            "AMAZON_APP_ID must look like amzn1.sp.solution.<uuid>",
        ),
        (
            {"AMAZON_APP_ID": AMAZON_APP, "AMAZON_CLIENT_ID": ""},
            "AMAZON_APP_ID set needs AMAZON_CLIENT_ID",
        ),
        (
            {
                "AMAZON_APP_ID": AMAZON_APP,
                "AMAZON_CLIENT_ID": AMAZON_CLIENT,
                "AMAZON_AUTHORIZE_URL": "",
            },
            "AMAZON_APP_ID set needs AMAZON_AUTHORIZE_URL as an https URL",
        ),
        (
            {
                "AMAZON_APP_ID": AMAZON_APP,
                "AMAZON_CLIENT_ID": AMAZON_CLIENT,
                "AMAZON_AUTHORIZE_URL": "http://sellercentral.amazon.in/apps/authorize/consent",
            },
            "AMAZON_APP_ID set needs AMAZON_AUTHORIZE_URL as an https URL",
        ),
        (
            {
                "AMAZON_APP_ID": AMAZON_APP,
                "AMAZON_CLIENT_ID": AMAZON_CLIENT,
                # quoted: the env file is sourced, so only a quoted space reaches the check
                "AMAZON_AUTHORIZE_URL": '"' + AMAZON_CONSENT.replace("?", " ?") + '"',
            },
            "AMAZON_APP_ID set needs AMAZON_AUTHORIZE_URL as an https URL",
        ),
        (
            {"AMAZON_APP_ID": AMAZON_APP + "x", "AMAZON_CLIENT_ID": AMAZON_CLIENT},
            "AMAZON_APP_ID must look like amzn1.sp.solution.<uuid>",
        ),
        (
            {"AMAZON_APP_ID": "x" + AMAZON_APP, "AMAZON_CLIENT_ID": AMAZON_CLIENT},
            "AMAZON_APP_ID must look like amzn1.sp.solution.<uuid>",
        ),
    ],
)
def test_bad_amazon_app_refused(
    toolkit: Path, tmp_path: Path, values: dict[str, str], message: str
) -> None:
    _set_env(toolkit, "staging", **values)
    for command in ("release", "deploy", "setup"):
        result = _run(toolkit, tmp_path, "staging", command, "--dry-run", "--sha", SHA)
        assert result.returncode != 0, command
        assert message in result.stderr, command


def test_release_runs_the_amazon_guard_before_building(repo_root: Path) -> None:
    script = (repo_root / DEPLOY / "deploy.sh").read_text(encoding="utf-8")
    for name in ("cmd_release() {", "cmd_deploy() {"):
        body = script[script.index(name) :]
        body = body[: body.index("\n}\n")]
        assert "amazon_guard" in body, name
    release = script[script.index("cmd_release() {") :]
    release = release[: release.index("\n}\n")]
    assert release.index("amazon_guard") < release.index("cmd_build")


@pytest.mark.parametrize("command", ["deploy", "release"])
def test_deploy_refuses_without_the_amazon_secret(
    toolkit: Path, tmp_path: Path, command: str
) -> None:
    """With the Amazon app on, an AMAZON_CLIENT_SECRET with no version stops the run before
    anything is built or deployed, naming the secret and the fix."""
    _amazon_on(toolkit, "staging")
    result, log = _run_model_key(
        toolkit, tmp_path, command, has_key=False, missing="AMAZON_CLIENT_SECRET"
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "secret AMAZON_CLIENT_SECRET has no value in aimos-staging" in result.stderr
    assert "deploy.sh staging setup first" in result.stderr
    assert not log.exists(), log.read_text(encoding="utf-8")  # nothing built or deployed


def test_amazon_guard_passes_with_the_secret(toolkit: Path, tmp_path: Path) -> None:
    _amazon_on(toolkit, "staging")
    result, _ = _run_model_key(
        toolkit, tmp_path, "deploy", has_key=True, missing="AMAZON_CLIENT_SECRET"
    )
    assert "deploy: amazon guard: OK (AMAZON_CLIENT_SECRET)" in result.stdout, result.stderr


# ---- ADR-0024: the real pipeline build and the generation route ----

PIPELINE_SHA = "f61f48d" + "0" * 31 + "aa"
PIPELINE_KEY = "MKOS_PIPELINE_DEPLOY_KEY"
MODEL_KEYS = ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY")
NO_ROUTER_MODEL = "LLM_GATEWAY=openrouter needs LLM_MODEL_DEFAULT as an OpenRouter model id"


def _commit_toolkit(toolkit: Path) -> None:
    """The toolkit copy as one clean commit, so `build` (which refuses a dirty tree) runs."""
    repo = toolkit.parents[2]
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    git += ["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
    if not (repo / ".git").exists():
        subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)


def _worker_container(out: str) -> tuple[dict[str, str], list[str]]:
    spec: dict[str, Any] = _worker_spec(out)
    container = spec["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in container["env"] if "value" in e}
    return env, [e["name"] for e in container["env"] if "valueFrom" in e]


def test_env_files_carry_the_pipeline_and_the_gateway(repo_root: Path) -> None:
    """prod routes through OpenRouter with the worker on the pinned pipeline (v0.1.0, 2026-11-06);
    staging keeps the stub, origin's direct OpenAI route and the Settings defaults."""
    found = {}
    for env_name in ("staging", "prod"):
        text = (repo_root / DEPLOY / f"{env_name}.env").read_text(encoding="utf-8")
        found[env_name] = dict(re.findall(r"^([A-Z_]+)=(.*)$", text, re.MULTILINE))
        values = found[env_name]
        for name in (PIPELINE_KEY, "LLM_API_BASE", *MODEL_KEYS):
            assert name not in values, (env_name, name)
    prod, staging = found["prod"], found["staging"]
    assert (prod["LLM_GATEWAY"], prod["LLM_PROVIDER"]) == ("openrouter", "deepseek")
    assert prod["LLM_MODEL_DEFAULT"] == "deepseek/deepseek-v4-flash"
    assert (prod["LLM_DAILY_BUDGET_USD"], prod["LLM_TENANT_DAILY_BUDGET_USD"]) == ("3", "1")
    # SI5: 600 requests a minute per tenant on the api
    assert prod["RATE_LIMIT_PER_TENANT_PER_MIN"] == "600"
    assert staging["RATE_LIMIT_PER_TENANT_PER_MIN"] == ""
    # prod pins a full mkos-pipeline commit (ADR-0024), so its worker runs the real pipeline
    assert re.fullmatch(r"[0-9a-f]{40}", prod["PIPELINE_REF"])
    assert prod["WORKER_ENABLED"] == "true"
    assert staging["PIPELINE_REF"] == ""
    assert (staging["LLM_GATEWAY"], staging["LLM_PROVIDER"]) == ("direct", "openai")
    for name in ("LLM_MODEL_DEFAULT", "LLM_DAILY_BUDGET_USD", "LLM_TENANT_DAILY_BUDGET_USD"):
        assert staging[name] == "", name
    # SI12: the size-chart read keeps LLM_MODEL_VISION (gpt-4o in prod). It is on in prod since
    # pipeline v0.6.0 met every G-size bar live (an unverified unit comes back unknown) and off on staging
    assert prod["LLM_MODEL_VISION_SIZE_CHART"] == staging["LLM_MODEL_VISION_SIZE_CHART"] == ""
    assert (prod["SIZE_CHART_READ_ENABLED"], staging["SIZE_CHART_READ_ENABLED"]) == (
        "true",
        "false",
    )
    assert (prod["LLM_MODEL_VISION"], staging["LLM_MODEL_VISION"]) == ("gpt-4o", "")
    assert prod["PIPELINE_REF"] == "bf4fbb8afa2562a0bf886f8038bd8813016a9084"
    assert staging["WORKER_ENABLED"] == "true"


def test_size_chart_model_goes_to_the_worker_only_when_set(toolkit: Path, tmp_path: Path) -> None:
    """SI12: blank (as committed) sends nothing, so the worker keeps LLM_MODEL_VISION."""
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    assert "LLM_MODEL_VISION_SIZE_CHART" not in _worker_container(out)[0]
    assert "LLM_MODEL_VISION" not in _worker_container(out)[0]
    _set_env(toolkit, "staging", LLM_MODEL_VISION="gpt-4o", LLM_MODEL_VISION_SIZE_CHART="gpt-4o")
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    worker = _worker_container(out)[0]
    assert worker["LLM_MODEL_VISION"] == worker["LLM_MODEL_VISION_SIZE_CHART"] == "gpt-4o"
    assert "LLM_MODEL_VISION_SIZE_CHART" not in _api_env(out)
    assert "LLM_MODEL_VISION" not in _api_env(out)


def test_size_chart_read_switch_reaches_both_roles(toolkit: Path, tmp_path: Path) -> None:
    """SI12: the api's ask and the worker's job read one switch; off as committed."""
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    assert _api_env(out)["SIZE_CHART_READ_ENABLED"] == "false"
    assert _worker_container(out)[0]["SIZE_CHART_READ_ENABLED"] == "false"
    _set_env(toolkit, "staging", SIZE_CHART_READ_ENABLED="true")
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    assert _api_env(out)["SIZE_CHART_READ_ENABLED"] == "true"
    assert _worker_container(out)[0]["SIZE_CHART_READ_ENABLED"] == "true"
    _set_env(toolkit, "staging", SIZE_CHART_READ_ENABLED="yes")
    result = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA)
    assert result.returncode != 0
    assert "SIZE_CHART_READ_ENABLED must be true or false" in result.stderr


def test_staging_worker_keeps_the_direct_route(toolkit: Path, tmp_path: Path) -> None:
    """Staging as committed: the OpenAI key, no router key, no model or caps in the env."""
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    env, secrets = _worker_container(out)
    assert (env["LLM_GATEWAY"], env["LLM_PROVIDER"]) == ("direct", "openai")
    for name in ("LLM_MODEL_DEFAULT", "LLM_DAILY_BUDGET_USD", "LLM_API_BASE"):
        assert name not in env, name
    assert "OPENAI_API_KEY" in secrets
    assert "OPENROUTER_API_KEY" not in secrets and "DEEPSEEK_API_KEY" not in secrets
    setup = _run(toolkit, tmp_path, "staging", "setup", "--dry-run", "--sha", SHA).stdout
    for name in ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"):
        assert f"secrets create {name} " not in setup and f"{name}: " not in setup, name
        assert name not in _secret_grants(setup, "aimos-worker"), name


def test_tenant_rate_reaches_the_api_only(toolkit: Path, tmp_path: Path) -> None:
    """SI5: a set per-tenant rate is in the api env and not the worker's (the worker serves no
    HTTP); blank (staging as committed) omits it. prod.env's 600 is pinned above."""
    plain = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    assert "RATE_LIMIT_PER_TENANT_PER_MIN" not in _api_env(plain)
    _set_env(toolkit, "staging", RATE_LIMIT_PER_TENANT_PER_MIN="600")
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    assert _api_env(out)["RATE_LIMIT_PER_TENANT_PER_MIN"] == "600"
    worker, _ = _worker_container(out)
    assert worker["APP_ROLE"] == "worker"
    assert "RATE_LIMIT_PER_TENANT_PER_MIN" not in worker


@pytest.mark.parametrize("value", ["abc", "0", "-5", "1.5", "6e2"])
def test_tenant_rate_must_be_digits(toolkit: Path, tmp_path: Path, value: str) -> None:
    _set_env(toolkit, "staging", RATE_LIMIT_PER_TENANT_PER_MIN=value)
    failed = _run_fail(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA)
    assert "RATE_LIMIT_PER_TENANT_PER_MIN" in failed.stderr + failed.stdout


def _openrouter_staging(toolkit: Path) -> None:
    _set_env(
        toolkit,
        "staging",
        LLM_GATEWAY="openrouter",
        LLM_PROVIDER="deepseek",
        LLM_MODEL_DEFAULT="deepseek/deepseek-chat",
        LLM_DAILY_BUDGET_USD="3",
        LLM_TENANT_DAILY_BUDGET_USD="1",
    )


def test_openrouter_worker_gets_the_gateway_and_one_key(
    toolkit: Path, tmp_path: Path, clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under openrouter the worker env carries the gateway, provider, model and caps, and its
    only model secret is OPENROUTER_API_KEY (on both deploy paths); the env boots without the
    OpenAI key."""
    _openrouter_staging(toolkit)
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    env, secrets = _worker_container(out)
    assert (env["LLM_GATEWAY"], env["LLM_PROVIDER"]) == ("openrouter", "deepseek")
    assert env["LLM_MODEL_DEFAULT"] == "deepseek/deepseek-chat"
    assert (env["LLM_DAILY_BUDGET_USD"], env["LLM_TENANT_DAILY_BUDGET_USD"]) == ("3", "1")
    assert "LLM_API_BASE" not in env and "openrouter.ai" not in "".join(env.values())
    assert [name for name in secrets if name in MODEL_KEYS] == ["OPENROUTER_API_KEY"]
    flags = _run(
        toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA, worker_deploy=True
    ).stdout
    line = next(line for line in flags.splitlines() if "run worker-pools deploy" in line)
    assert [name for name in _secret_names(line) if name in MODEL_KEYS] == ["OPENROUTER_API_KEY"]
    worker_env = yaml.safe_load(_blocks(flags, "env file")[1])
    assert worker_env["LLM_GATEWAY"] == "openrouter" and worker_env["APP_ROLE"] == "worker"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _boot(monkeypatch, env, secrets)
    from app.core.config import get_settings

    loaded = get_settings()
    assert (loaded.llm_gateway, loaded.llm_provider) == ("openrouter", "deepseek")
    assert loaded.openai_api_key is None
    assert (loaded.llm_daily_budget_usd, loaded.llm_tenant_daily_budget_usd) == (3.0, 1.0)


def test_api_gets_the_caps_but_no_model_route_or_key(toolkit: Path, tmp_path: Path) -> None:
    """The api's generate precheck and the worker refuse at the same caps; only the worker calls
    a model, so only it gets the gateway, the provider, the model and the key."""
    _openrouter_staging(toolkit)
    for env_name in ("staging", "prod"):
        out = _run(toolkit, tmp_path, env_name, "deploy", "--dry-run", "--sha", SHA).stdout
        env = _api_env(out)
        assert (env["LLM_DAILY_BUDGET_USD"], env["LLM_TENANT_DAILY_BUDGET_USD"]) == ("3", "1")
        for name in ("LLM_GATEWAY", "LLM_PROVIDER", "LLM_MODEL_DEFAULT"):
            assert name not in env, (env_name, name)
        deploy = next(line for line in out.splitlines() if line.startswith("+ gcloud run deploy "))
        assert not set(_secret_names(deploy)) & set(MODEL_KEYS), env_name
        maintenance = _blocks(out, "env file")[-1]
        assert "LLM_" not in maintenance


def test_direct_deepseek_adds_its_own_key(toolkit: Path, tmp_path: Path) -> None:
    _set_env(toolkit, "staging", LLM_PROVIDER="deepseek")
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    env, secrets = _worker_container(out)
    assert (env["LLM_GATEWAY"], env["LLM_PROVIDER"]) == ("direct", "deepseek")
    assert {"OPENAI_API_KEY", "DEEPSEEK_API_KEY"} <= set(secrets)
    assert "OPENROUTER_API_KEY" not in secrets
    setup = _run(toolkit, tmp_path, "staging", "setup", "--dry-run", "--sha", SHA).stdout
    assert 'bash -c \'read -rsp "DEEPSEEK_API_KEY: " v; echo; printf %s "$v" | ' in setup


def test_setup_asks_for_the_router_key_with_the_worker(toolkit: Path, tmp_path: Path) -> None:
    _openrouter_staging(toolkit)
    out = _run(toolkit, tmp_path, "staging", "setup", "--dry-run", "--sha", SHA).stdout
    assert 'bash -c \'read -rsp "OPENROUTER_API_KEY: " v; echo; printf %s "$v" | ' in out
    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        assert f"{name}: " not in out, name
    assert "secrets create OPENROUTER_API_KEY " in out
    assert "OPENROUTER_API_KEY" in _secret_grants(out, "aimos-worker")
    assert "OPENROUTER_API_KEY" not in _secret_grants(out, "aimos-api")
    assert not {"OPENAI_API_KEY", "DEEPSEEK_API_KEY"} & _secret_grants(out, "aimos-worker")


def test_prod_setup_with_the_worker_off_asks_for_no_model_key(
    toolkit: Path, tmp_path: Path
) -> None:
    """prod.env is openrouter with the worker off: setup creates the empty secret and grants it to
    the worker, but asks for no value, so setup does not stop on it."""
    out = _run(toolkit, tmp_path, "prod", "setup", "--dry-run", "--sha", SHA).stdout
    assert "secrets create OPENROUTER_API_KEY " in out
    assert "OPENROUTER_API_KEY" in _secret_grants(out, "aimos-worker")
    assert "OPENROUTER_API_KEY: " not in out
    assert "secret OPENROUTER_API_KEY has no value" not in out


def _secret_revokes(out: str, account: str) -> set[str]:
    revokes = set()
    for line in out.splitlines():
        if "secrets remove-iam-policy-binding" in line and f"serviceAccount:{account}@" in line:
            words = shlex.split(line)
            assert "--role=roles/secretmanager.secretAccessor" in words, line
            revokes.add(words[4])
    return revokes


@pytest.mark.parametrize(
    ("env_name", "values", "kept"),
    [
        ("staging", {}, {"OPENAI_API_KEY"}),
        ("staging", {"LLM_PROVIDER": "deepseek"}, {"OPENAI_API_KEY", "DEEPSEEK_API_KEY"}),
        (
            "staging",
            {"LLM_GATEWAY": "openrouter", "LLM_MODEL_DEFAULT": "deepseek/deepseek-chat"},
            {"OPENROUTER_API_KEY"},
        ),
        ("prod", {}, {"OPENROUTER_API_KEY"}),
        ("prod", {"WORKER_ENABLED": "true", "PIPELINE_REF": PIPELINE_SHA}, {"OPENROUTER_API_KEY"}),
    ],
)
def test_setup_removes_the_worker_access_to_the_other_gateway_keys(
    toolkit: Path, tmp_path: Path, env_name: str, values: dict[str, str], kept: set[str]
) -> None:
    """A gateway switch: an earlier setup granted the worker the other gateway's keys (every
    worker had OPENAI_API_KEY before ADR-0024, prod included), and --set-secrets only stops
    mounting them, so setup removes the worker's access to every model key it no longer uses,
    after the grants, and never touches another account."""
    _set_env(toolkit, env_name, **values)
    out = _run(toolkit, tmp_path, env_name, "setup", "--dry-run", "--sha", SHA).stdout
    model_grants = _secret_grants(out, "aimos-worker") & set(MODEL_KEYS)
    assert model_grants == kept
    assert _secret_revokes(out, "aimos-worker") == set(MODEL_KEYS) - kept
    revokes = [x for x in out.splitlines() if "secrets remove-iam-policy-binding" in x]
    assert len(revokes) == len(MODEL_KEYS) - len(kept)
    lines = out.splitlines()
    last_grant = max(i for i, x in enumerate(lines) if "aimos-worker@" in x and "add-iam" in x)
    assert last_grant < lines.index(revokes[0])


def _revoke_harness(toolkit: Path, tmp_path: Path, policy: str, model_keys: str) -> list[str]:
    """revoke_stale_model_keys and secret_grants_accessor out of deploy.sh, run for real (no dry
    run) against a stub `gq` that prints POLICY (csv role,member) for OPENAI_API_KEY and fails
    for any other secret, as gcloud does for a missing one; returns the `gc` calls."""
    script = (toolkit / "deploy.sh").read_text(encoding="utf-8")
    bodies = []
    for name in ("revoke_stale_model_keys() {", "secret_grants_accessor() {"):
        body = script[script.index(name) :]
        bodies.append(body[: body.index("\n}\n") + 3])
    harness = tmp_path / "revoke.sh"
    harness.write_text(
        "set -euo pipefail\n"
        "DRY_RUN=0 PROJECT=p SA_WORKER=aimos-worker\n"
        'ALL_MODEL_KEYS="OPENAI_API_KEY DEEPSEEK_API_KEY OPENROUTER_API_KEY"\n'
        f'MODEL_KEYS="{model_keys}"\n'
        'sa_email() { printf \'%s@%s.iam.gserviceaccount.com\' "$1" "$PROJECT"; }\n'
        'retry() { "$@"; }\n'
        'gc() { echo "gc $*"; }\n'
        'gq() { [ "$3" = OPENAI_API_KEY ] || return 1; printf "%s" "$POLICY"; }\n'
        + "".join(bodies)
        + "revoke_stale_model_keys\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(harness)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        env={"PATH": os.environ["PATH"], "POLICY": policy},
    )
    return result.stdout.splitlines()


WORKER = "serviceAccount:aimos-worker@p.iam.gserviceaccount.com"


def test_revoke_runs_only_where_the_worker_holds_the_binding(toolkit: Path, tmp_path: Path) -> None:
    """A real run removes the binding only on a secret that exists and grants the worker
    secretAccessor; a missing secret, another account or another role is left alone."""
    held = f"roles/secretmanager.secretAccessor,{WORKER}\nroles/x,serviceAccount:other@p\n"
    calls = _revoke_harness(toolkit, tmp_path, held, "OPENROUTER_API_KEY")
    assert calls == [
        f"gc secrets remove-iam-policy-binding OPENAI_API_KEY --member={WORKER} "
        "--role=roles/secretmanager.secretAccessor --format=none"
    ]
    for policy in (
        "",
        f"roles/secretmanager.viewer,{WORKER}\n",
        "roles/secretmanager.secretAccessor,serviceAccount:aimos-api@p.iam.gserviceaccount.com\n",
        f"roles/secretmanager.secretAccessor,{WORKER}x\n",
    ):
        assert _revoke_harness(toolkit, tmp_path, policy, "OPENROUTER_API_KEY") == [], policy
    # the gateway's own key is never removed
    assert _revoke_harness(toolkit, tmp_path, held, "OPENAI_API_KEY") == []


@pytest.mark.parametrize(
    ("env_name", "values", "message"),
    [
        (
            "prod",
            {"WORKER_ENABLED": "true"},
            "WORKER_ENABLED=true in production needs PIPELINE_REF",
        ),
        ("prod", {"PIPELINE_REF": "f61f48d"}, "PIPELINE_REF must be a full 40-character"),
        ("staging", {"PIPELINE_REF": "F" * 40}, "PIPELINE_REF must be a full 40-character"),
        ("staging", {"PIPELINE_REF": "v0.1.0"}, "PIPELINE_REF must be a full 40-character"),
        ("staging", {"LLM_PROVIDER": "openrouter"}, "LLM_PROVIDER must be openai or deepseek"),
        ("staging", {"LLM_GATEWAY": "https://x.test/v1"}, "LLM_GATEWAY must be direct or"),
        (
            "prod",
            {"LLM_GATEWAY": "OpenRouter", "IMAGE_EDIT_ENABLED": "false"},
            "LLM_GATEWAY must be direct or openrouter",
        ),
        ("prod", {"LLM_MODEL_DEFAULT": ""}, NO_ROUTER_MODEL),
        ("prod", {"LLM_MODEL_DEFAULT": "deepseek-chat"}, NO_ROUTER_MODEL),
        ("prod", {"LLM_MODEL_DEFAULT": "deepseek/"}, NO_ROUTER_MODEL),
        ("staging", {"LLM_GATEWAY": "openrouter"}, NO_ROUTER_MODEL),
    ],
)
def test_bad_pipeline_or_model_settings_refused_before_gcloud(
    toolkit: Path, tmp_path: Path, env_name: str, values: dict[str, str], message: str
) -> None:
    _set_env(toolkit, env_name, **values)
    for command in ("build", "release", "deploy", "setup"):
        result = _run(toolkit, tmp_path, env_name, command, "--dry-run", "--sha", SHA)
        assert result.returncode == 1, command
        assert message in result.stderr, command
        assert "+ gcloud" not in result.stdout, command


def test_prod_worker_with_a_pipeline_ref_deploys_and_boots(
    toolkit: Path, tmp_path: Path, clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With PIPELINE_REF the guard lets the prod worker through, and its env (prod.env's gateway
    values, the router key alone) boots once the image holds mkos-pipeline instead of the stub."""
    _set_env(toolkit, "prod", WORKER_ENABLED="true", PIPELINE_REF=PIPELINE_SHA)
    out = _run(toolkit, tmp_path, "prod", "deploy", "--dry-run", "--sha", SHA).stdout
    env, secrets = _worker_container(out)
    assert env["APP_ENV"] == "production" and env["LLM_GATEWAY"] == "openrouter"
    assert "PIPELINE_REF" not in env and PIPELINE_KEY not in secrets
    assert [name for name in secrets if name in MODEL_KEYS] == ["OPENROUTER_API_KEY"]
    monkeypatch.setattr("app.core.config._pipeline_stub_installed", lambda: False)
    _boot(monkeypatch, env, secrets)


def test_build_without_a_pipeline_ref_is_the_stub_build(toolkit: Path, tmp_path: Path) -> None:
    _commit_toolkit(toolkit)
    out = _run(toolkit, tmp_path, "staging", "build", "--dry-run").stdout
    submit = next(line for line in out.splitlines() if "builds submit" in line)
    assert re.search(r"--config=\S+/cloudbuild\.yaml ", submit), submit
    assert "_PIPELINE_REF" not in submit and "pipeline guard" not in out


def test_build_with_a_pipeline_ref_uses_the_pipeline_config(toolkit: Path, tmp_path: Path) -> None:
    _set_env(toolkit, "staging", PIPELINE_REF=PIPELINE_SHA)
    _commit_toolkit(toolkit)
    out = _run(toolkit, tmp_path, "staging", "build", "--dry-run").stdout
    submit = next(line for line in out.splitlines() if "builds submit" in line)
    assert re.search(r"--config=\S+/cloudbuild-pipeline\.yaml ", submit), submit
    subs = re.search(r"--substitutions=(\S+)", submit)
    assert subs is not None
    keys = dict(pair.split("=", 1) for pair in subs.group(1).split(","))
    assert set(keys) == {"_IMAGE", "_GIT_SHA", "_PIPELINE_REF"}
    assert keys["_PIPELINE_REF"] == PIPELINE_SHA
    assert "deploy: pipeline guard: skipped (dry run)" in out
    assert "aimos-build@aimos-staging.iam.gserviceaccount.com" in submit


FAKE_GCLOUD_NO_KEY = """#!/bin/sh
case " $* " in
  *" config get-value account "*) echo "owner@example.test"; exit 0 ;;
  *" config get-value project "*) echo "aimos-staging"; exit 0 ;;
  *" billing projects describe "*) echo "True"; exit 0 ;;
  *" services list "*) exit 0 ;;
  *" artifacts docker images describe "*) echo "ERROR: NOT_FOUND" >&2; exit 1 ;;
  *" secrets versions list "*) exit 0 ;;
esac
echo "$*" >> "$FAKE_GCLOUD_LOG"
exit 98
"""


def test_build_refuses_without_the_deploy_key(toolkit: Path, tmp_path: Path) -> None:
    """A pipeline build stops before the upload when the deploy key has no value, and prints the
    owner's command (the key's value never passes through the script)."""
    _set_env(toolkit, "staging", PIPELINE_REF=PIPELINE_SHA)
    _commit_toolkit(toolkit)
    bin_dir = tmp_path / "bin-no-key"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(FAKE_GCLOUD_NO_KEY, encoding="utf-8")
    (bin_dir / "gcloud").chmod(0o755)
    log = tmp_path / "gcloud-no-key.log"
    result = subprocess.run(
        ["bash", str(toolkit / "deploy.sh"), "staging", "build"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "FAKE_GCLOUD_LOG": str(log),
        },
        cwd=tmp_path,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"secret {PIPELINE_KEY} has no value in aimos-staging" in result.stderr
    assert f"gcloud secrets versions add {PIPELINE_KEY} --data-file=" in result.stdout
    assert "gh repo deploy-key add" in result.stdout and "--allow-write" not in result.stdout
    assert not log.exists(), log.read_text(encoding="utf-8")  # nothing submitted


def test_setup_with_a_pipeline_ref_grants_the_key_to_the_build_account_only(
    toolkit: Path, tmp_path: Path
) -> None:
    _set_env(toolkit, "staging", PIPELINE_REF=PIPELINE_SHA)
    out = _run(toolkit, tmp_path, "staging", "setup", "--dry-run", "--sha", SHA).stdout
    assert f"secrets create {PIPELINE_KEY} --replication-policy=user-managed" in out
    grants = [
        line
        for line in out.splitlines()
        if f"secrets add-iam-policy-binding {PIPELINE_KEY} " in line
    ]
    assert len(grants) == 1
    assert "--member=serviceAccount:aimos-build@aimos-staging.iam.gserviceaccount.com" in grants[0]
    assert "--role=roles/secretmanager.secretAccessor" in grants[0]
    assert f"secrets versions add {PIPELINE_KEY} --data-file=" in out
    # no value is written by the script itself
    assert f"| gcloud secrets versions add {PIPELINE_KEY}" not in out


def test_setup_without_a_pipeline_ref_never_touches_the_key(toolkit: Path, tmp_path: Path) -> None:
    for env_name in ("staging", "prod"):
        out = _run(toolkit, tmp_path, env_name, "setup", "--dry-run", "--sha", SHA).stdout
        assert PIPELINE_KEY not in out, env_name


def _cloudbuild(repo_root: Path, name: str) -> tuple[str, dict[str, Any]]:
    text = (repo_root / DEPLOY / name).read_text(encoding="utf-8")
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    return text, loaded


def _substitutions(loaded: dict[str, Any]) -> set[str]:
    """Every `${NAME}` Cloud Build substitutes in the config (`$$` is a literal `$`)."""
    flat = yaml.safe_dump(loaded).replace("$$", "")
    return set(re.findall(r"\$\{([A-Z_]+)\}", flat))


def test_stub_cloudbuild_config_has_no_secret(repo_root: Path) -> None:
    text, loaded = _cloudbuild(repo_root, "cloudbuild.yaml")
    assert "availableSecrets" not in loaded and "secretEnv" not in text
    assert "PIPELINE" not in text
    assert _substitutions(loaded) == {"_IMAGE", "_GIT_SHA"}


def test_pipeline_cloudbuild_config_wires_the_key_as_a_buildkit_secret(repo_root: Path) -> None:
    _, loaded = _cloudbuild(repo_root, "cloudbuild-pipeline.yaml")
    # deploy.sh passes exactly these user substitutions (Cloud Build refuses a mismatch)
    assert _substitutions(loaded) == {"_IMAGE", "_GIT_SHA", "_PIPELINE_REF", "PROJECT_ID"}
    (secret,) = loaded["availableSecrets"]["secretManager"]
    assert secret == {
        "versionName": f"projects/${{PROJECT_ID}}/secrets/{PIPELINE_KEY}/versions/latest",
        "env": PIPELINE_KEY,
    }
    (step,) = loaded["steps"]
    assert step["name"] == "gcr.io/cloud-builders/docker" and step["entrypoint"] == "bash"
    assert step["secretEnv"] == [PIPELINE_KEY] and step["env"] == ["DOCKER_BUILDKIT=1"]
    assert loaded["images"] == ["${_IMAGE}"]
    script = step["args"][1]
    assert step["args"][0] == "-c" and script.startswith("set -euo pipefail\n")
    for flag in (
        "--file=apps/api/Dockerfile",
        "--tag=${_IMAGE}",
        "--build-arg=PIPELINE_REF=${_PIPELINE_REF}",
        '--secret=id=mkos_pipeline_key,src="$$key"',
    ):
        assert flag in script, flag
    # the key goes to a temp file in the step container, removed on exit, never echoed
    assert "trap 'rm -f \"$$key\"' EXIT" in script
    assert f'printf \'%s\\n\' "$${PIPELINE_KEY}" >"$$key"' in script
    assert "set -x" not in script and "echo" not in script and "/workspace" not in script
    assert PIPELINE_KEY not in script.split("docker build", 1)[1]


def test_pipeline_cloudbuild_script_renders_to_valid_bash(repo_root: Path, tmp_path: Path) -> None:
    """The step script after Cloud Build's substitution parses as bash and names the Dockerfile
    secret id."""
    _, loaded = _cloudbuild(repo_root, "cloudbuild-pipeline.yaml")
    script = loaded["steps"][0]["args"][1]
    rendered = (
        script.replace("${_IMAGE}", f"asia-south1-docker.pkg.dev/p/aimos/backend:sha-{SHA}")
        .replace("${_GIT_SHA}", SHA)
        .replace("${_PIPELINE_REF}", PIPELINE_SHA)
        .replace("$$", "$")
    )
    assert "${_" not in rendered
    path = tmp_path / "step.sh"
    path.write_text(rendered, encoding="utf-8")
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    dockerfile = (repo_root / "apps/api/Dockerfile").read_text(encoding="utf-8")
    assert "--mount=type=secret,id=mkos_pipeline_key," in dockerfile
    assert "--secret=id=mkos_pipeline_key," in rendered


def test_runbook_and_adr_cover_the_pipeline_build(repo_root: Path) -> None:
    runbook = (repo_root / "docs/runbooks/DEPLOY-GCP.md").read_text(encoding="utf-8")
    for needle in ("PIPELINE_REF", PIPELINE_KEY, "cloudbuild-pipeline.yaml", "LLM_GATEWAY"):
        assert needle in runbook, needle
    assert "OPENROUTER_API_KEY" in runbook and "LLM_API_BASE" not in runbook
    adr = (repo_root / "docs/adr/0024-real-pipeline-deploy-build-install.md").read_text(
        encoding="utf-8"
    )
    assert "T104" in adr and PIPELINE_KEY in adr and "LLM_GATEWAY" in adr


# ---- the model key guard: a deploy refuses before the api moves when the worker's key is empty ----

FAKE_GCLOUD_NO_MODEL_KEY = """#!/bin/sh
case " $* " in
  *" --help "*) exit 0 ;;
  *" config get-value account "*) echo "owner@example.test"; exit 0 ;;
  *" config get-value project "*) echo "aimos-staging"; exit 0 ;;
  *" billing projects describe "*) echo "True"; exit 0 ;;
  *"run services describe"*) echo "ERROR: NOT_FOUND" >&2; exit 1 ;;
  *"kms keys describe"*) echo "projects/p/cryptoKeys/k"; exit 0 ;;
  *"iam service-accounts describe"*) echo "sa@p"; exit 0 ;;
  *"services list"*) echo "cloudscheduler.googleapis.com"; exit 0 ;;
  *"secrets versions list $MISSING_KEY "*) [ -n "$HAS_MODEL_KEY" ] && echo "versions/1"; exit 0 ;;
  *"secrets versions list "*) echo "versions/1"; exit 0 ;;
esac
echo "$*" >> "$FAKE_GCLOUD_LOG"
exit 98
"""


def _run_model_key(
    toolkit: Path,
    tmp_path: Path,
    command: str,
    *,
    has_key: bool,
    missing: str = "OPENROUTER_API_KEY",
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Staging under openrouter, or as committed (direct) when `missing` is a direct key."""
    if missing == "OPENROUTER_API_KEY":
        _openrouter_staging(toolkit)
    elif missing == "DEEPSEEK_API_KEY":
        _set_env(toolkit, "staging", LLM_PROVIDER="deepseek")
    bin_dir = tmp_path / f"bin-model-{command}-{has_key}"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(FAKE_GCLOUD_NO_MODEL_KEY, encoding="utf-8")
    (bin_dir / "gcloud").chmod(0o755)
    log = tmp_path / f"gcloud-model-{command}-{has_key}.log"
    result = subprocess.run(
        ["bash", str(toolkit / "deploy.sh"), "staging", command, "--sha", SHA],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "FAKE_GCLOUD_LOG": str(log),
            "HAS_MODEL_KEY": "1" if has_key else "",
            "MISSING_KEY": missing,
        },
        cwd=tmp_path,
    )
    return result, log


@pytest.mark.parametrize("command", ["deploy", "release"])
def test_deploy_refuses_without_the_generation_key(
    toolkit: Path, tmp_path: Path, command: str
) -> None:
    """Staging switched to openrouter with the worker on: a project whose `setup` predates
    OPENROUTER_API_KEY stops before the build and before the api moves, naming the secret and
    the fix."""
    result, log = _run_model_key(toolkit, tmp_path, command, has_key=False)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "secret OPENROUTER_API_KEY has no value in aimos-staging" in result.stderr
    assert "deploy.sh staging setup first" in result.stderr
    assert not log.exists(), log.read_text(encoding="utf-8")  # nothing built or deployed


@pytest.mark.parametrize("missing", ["OPENAI_API_KEY", "DEEPSEEK_API_KEY"])
def test_direct_deploy_refuses_without_its_model_key(
    toolkit: Path, tmp_path: Path, missing: str
) -> None:
    """Under direct with the worker on, an empty OpenAI key (or DeepSeek's, when it generates)
    stops the deploy before anything moves."""
    result, log = _run_model_key(toolkit, tmp_path, "deploy", has_key=False, missing=missing)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"secret {missing} has no value in aimos-staging" in result.stderr
    assert not log.exists(), log.read_text(encoding="utf-8")


def test_model_key_guard_passes_with_the_generation_key(toolkit: Path, tmp_path: Path) -> None:
    result, _ = _run_model_key(toolkit, tmp_path, "deploy", has_key=True)
    assert "deploy: model key guard: OK (OPENROUTER_API_KEY)" in result.stdout, result.stderr


def test_dry_run_deploy_reports_the_model_key_guard(toolkit: Path, tmp_path: Path) -> None:
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    lines = out.splitlines()
    guard = lines.index("deploy: model key guard: skipped (dry run)")
    assert lines.index("deploy: google guard: sign-in with Google is off") < guard
    assert guard < next(i for i, x in enumerate(lines) if "gcloud run deploy aimos-api" in x)
    # prod keeps the worker off, so its deploy never needs the key
    out = _run(toolkit, tmp_path, "prod", "deploy", "--dry-run", "--sha", SHA).stdout
    assert (
        "deploy: model key guard: not needed (WORKER_ENABLED=false, LLM_GATEWAY=openrouter)" in out
    )


def test_release_runs_the_model_key_guard_before_building(repo_root: Path) -> None:
    script = (repo_root / DEPLOY / "deploy.sh").read_text(encoding="utf-8")
    for name in ("cmd_release() {", "cmd_deploy() {"):
        body = script[script.index(name) :]
        body = body[: body.index("\n}\n")]
        assert "model_key_guard" in body, name
    release = script[script.index("cmd_release() {") :]
    release = release[: release.index("\n}\n")]
    assert release.index("model_key_guard") < release.index("cmd_build")
    deploy = script[script.index("cmd_deploy() {") :]
    deploy = deploy[: deploy.index("\n}\n")]
    assert deploy.index("model_key_guard") < deploy.index("deploy_api")


# ---- DW2: the PTD job (Amazon product type definitions in prod) ----


def _run_fail(toolkit: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = _run(toolkit, tmp_path, *args)
    assert result.returncode == 1, result.stdout + result.stderr
    return result


PTD_SECRET_NAMES = ["ALEMBIC_DATABASE_URL", "AMAZON_CLIENT_SECRET", "AMAZON_PLATFORM_REFRESH_TOKEN"]


def _ptd_deploy(out: str) -> str:
    return next(line for line in out.splitlines() if "run jobs deploy aimos-ptd" in line)


def _set_env_vars(command_line: str) -> dict[str, str]:
    match = re.search(r"--set-env-vars=\S*\^\|\^(\S+)", command_line.replace("\\", ""))
    assert match is not None, command_line
    return dict(pair.split("=", 1) for pair in match.group(1).split("|"))


def test_templates_refresh_runs_its_own_job_with_the_amazon_settings(
    toolkit: Path, tmp_path: Path, clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _amazon_on(toolkit, "prod")
    _set_env(toolkit, "prod", AMAZON_USE_SANDBOX="false")
    out = _run(
        toolkit, tmp_path, "prod", "templates", "refresh", "--product-type", "KURTA",
        "--staff-email", "s@b.test", "--reason", "first-load", "--dry-run", "--sha", SHA,
    ).stdout  # fmt: skip
    job = _ptd_deploy(out)
    # only this job mounts the platform token, next to the LWA secret and the migrator URL
    assert _secret_names(job) == PTD_SECRET_NAMES
    # its own identity: secret access is per identity, so the shared ops one never gets the token
    assert "--service-account=aimos-ptd@" in job
    env = _set_env_vars(job)
    assert env["APP_ROLE"] == "shell" and env["APP_ENV"] == "production"
    assert env["AMAZON_APP_ID"] == AMAZON_APP and env["AMAZON_CLIENT_ID"] == AMAZON_CLIENT
    assert env["AMAZON_USE_SANDBOX"] == "false"
    assert env["AMAZON_SPAPI_ENDPOINT"] == "https://sellingpartnerapi-eu.amazon.com"
    assert env["AMAZON_OAUTH_REDIRECT_URI"] == f"{API_URL}/api/v1/marketplaces/amazon/callback"
    args = job.replace("\\", "")
    assert (
        "--args=^|^-m|app.cli|templates|refresh|--product-type=KURTA|--staff-email=s@b.test"
        "|--reason=first-load" in args
    )
    # the PTD identity reads its three secrets; no other identity gets the platform token
    for name in PTD_SECRET_NAMES:
        assert any(
            f"secrets add-iam-policy-binding {name} " in line and "aimos-ptd@" in line
            for line in out.splitlines()
        ), name
    token_grants = [
        line
        for line in out.splitlines()
        if "secrets add-iam-policy-binding AMAZON_PLATFORM_REFRESH_TOKEN " in line
    ]
    assert token_grants and all("aimos-ptd@" in line for line in token_grants)
    assert "roles/cloudsql.client" in out
    assert "gcloud run jobs execute aimos-ptd" in out
    # the job's settings boot as the shell role in production
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", SECRET_VALUES["DATABASE_URL"])
    monkeypatch.setenv("AMAZON_CLIENT_SECRET", SECRET_VALUES["AMAZON_CLIENT_SECRET"])
    monkeypatch.setenv("AMAZON_PLATFORM_REFRESH_TOKEN", "test-only-platform-refresh-" + "9" * 8)
    from app.core.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    from app.cli import _missing_ptd_setting

    assert _missing_ptd_setting(settings) is None


def test_templates_promote_flattens_its_flags(toolkit: Path, tmp_path: Path) -> None:
    _amazon_on(toolkit, "prod")
    out = _run(
        toolkit, tmp_path, "prod", "templates", "promote-definition", "--product-type", "KURTA",
        "--version", "U1a2", "--digest", "abcd12", "--staff-email", "s@b.test", "--reason", "ok",
        "--dry-run", "--sha", SHA,
    ).stdout  # fmt: skip
    args = _ptd_deploy(out).replace("\\", "")
    assert (
        "|templates|promote-definition|--product-type=KURTA|--version=U1a2|--digest=abcd12"
        "|--staff-email=s@b.test|--reason=ok" in args
    )


def test_templates_refuses_when_the_amazon_app_is_off(toolkit: Path, tmp_path: Path) -> None:
    result = _run_fail(
        toolkit, tmp_path, "staging", "templates", "refresh", "--product-type", "KURTA",
        "--staff-email", "s@b.test", "--reason", "x", "--dry-run", "--sha", SHA,
    )  # fmt: skip
    assert "templates needs the Amazon app" in result.stderr
    assert "aimos-ptd" not in result.stdout


@pytest.mark.parametrize("action", ["diff", "load", "drop"])
def test_templates_refuses_other_actions(toolkit: Path, tmp_path: Path, action: str) -> None:
    _amazon_on(toolkit, "prod")
    result = _run_fail(toolkit, tmp_path, "prod", "templates", action, "--dry-run", "--sha", SHA)
    assert f"unknown templates action '{action}'" in result.stderr


def test_templates_refuses_a_separator_in_an_argument(toolkit: Path, tmp_path: Path) -> None:
    _amazon_on(toolkit, "prod")
    result = _run_fail(
        toolkit, tmp_path, "prod", "templates", "refresh", "--product-type", "KURTA",
        "--staff-email", "s@b.test", "--reason", "a|b", "--dry-run", "--sha", SHA,
    )  # fmt: skip
    assert "may not contain '|'" in result.stderr


def test_templates_keeps_a_comma_in_a_reason(toolkit: Path, tmp_path: Path) -> None:
    _amazon_on(toolkit, "prod")
    out = _run(
        toolkit, tmp_path, "prod", "templates", "refresh", "--product-type", "KURTA",
        "--staff-email", "s@b.test", "--reason", "first load, KURTA", "--dry-run", "--sha", SHA,
    ).stdout  # fmt: skip
    assert "|--reason=first load, KURTA" in _ptd_deploy(out).replace("\\", "")


def test_usage_prints_every_subcommand_line(repo_root: Path) -> None:
    script = (repo_root / DEPLOY / "deploy.sh").read_text(encoding="utf-8").splitlines()
    match = re.search(r"sed -n '5,(\d+)p'", "\n".join(script))
    assert match is not None
    last = int(match.group(1))
    # the header comment block ends right after the last subcommand line
    assert script[last - 1].startswith("#") and not script[last].startswith("#   ")
    assert script[last].strip() in ("#", "")


def test_cli_quiets_the_http_client_loggers() -> None:
    import logging

    from app.cli import quiet_http_clients
    from app.core.logging import _HTTP_CLIENT_LOGGERS

    for name in _HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)
    quiet_http_clients()
    for name in _HTTP_CLIENT_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name


# ---- SI1 (ADR-0026): the public image settings pass through only as a pair ----


def _append_env(toolkit: Path, env_name: str, **values: str) -> None:
    path = toolkit / f"{env_name}.env"
    extra = "".join(f"{key}={value}\n" for key, value in values.items())
    path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")


def test_public_image_settings_reach_every_env_file(toolkit: Path, tmp_path: Path) -> None:
    _append_env(
        toolkit,
        "staging",
        GCS_BUCKET_IMAGES_PUBLIC="aimos-images-public-staging",
        IMAGE_PUBLIC_BASE_URL="https://images-staging.kuinn.in",
    )
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    blocks = [yaml.safe_load(block) for block in _blocks(out, "env file")]
    assert len(blocks) >= 2  # the api and the maintenance job (and the worker when on)
    for env in blocks:
        assert env["GCS_BUCKET_IMAGES_PUBLIC"] == "aimos-images-public-staging"
        assert env["IMAGE_PUBLIC_BASE_URL"] == "https://images-staging.kuinn.in"


@pytest.mark.parametrize(
    "values",
    [{}, {"GCS_BUCKET_IMAGES_PUBLIC": "aimos-images-public-staging"}],
)
def test_public_image_settings_absent_unless_both_set(
    toolkit: Path, tmp_path: Path, values: dict[str, str]
) -> None:
    _append_env(toolkit, "staging", **values)
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    for block in _blocks(out, "env file"):
        env = yaml.safe_load(block)
        assert "GCS_BUCKET_IMAGES_PUBLIC" not in env and "IMAGE_PUBLIC_BASE_URL" not in env


def test_prod_pins_public_image_hosting() -> None:
    """prod.env carries both public image settings, on the images edge host (ADR-0026)."""
    env = dict(
        line.split("=", 1)
        for line in (Path(DEPLOY) / "prod.env").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    assert env["GCS_BUCKET_IMAGES_PUBLIC"] == "aimos-images-public-prod"
    assert env["IMAGE_PUBLIC_BASE_URL"] == "https://images.kuinn.in"


def test_images_edge_worker_serves_only_exact_public_names() -> None:
    """The images edge Worker accepts only `<26 base32>.<jpg|png>` on GET/HEAD (ADR-0026)."""
    src = Path("scripts/deploy/cloudflare/images-edge-worker.js").read_text(encoding="utf-8")
    assert r"const NAME = /^\/([a-z2-7]{26})\.(jpg|png)$/;" in src
    assert 'request.method !== "GET" && request.method !== "HEAD"' in src
    assert "url.hostname !== env.EDGE_HOST" in src


# ---- SI7 (CP-D21): the image-edit switch and limits reach both roles through app_env ----

IMAGE_EDIT_ROUTER = {
    "LLM_GATEWAY": "openrouter",
    "LLM_MODEL_DEFAULT": "deepseek/deepseek-chat",
    "IMAGE_EDIT_ENABLED": "true",
}


def _worker_env(out: str) -> tuple[dict[str, str], list[str]]:
    spec: dict[str, Any] = _worker_spec(out)
    container = spec["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in container["env"] if "value" in e}
    return env, [e["name"] for e in container["env"] if "valueFrom" in e]


@pytest.mark.parametrize("env_name", ["staging", "prod"])
def test_env_files_ship_image_edit_switch(repo_root: Path, env_name: str) -> None:
    text = (repo_root / DEPLOY / f"{env_name}.env").read_text(encoding="utf-8")
    values = dict(re.findall(r"^([A-Z_]+)=(.*)$", text, re.MULTILINE))
    # prod turned image editing on 2026-11-08 after the live check (DECISIONS D17); staging stays off.
    assert values["IMAGE_EDIT_ENABLED"] == ("true" if env_name == "prod" else "false")
    for key in (
        "LLM_MODEL_IMAGE_EDIT",
        "IMAGE_EDIT_TENANT_DAILY_LIMIT",
        "IMAGE_EDIT_PLATFORM_DAILY_SHARE",
    ):
        assert key in values, key


def test_image_edit_off_reaches_both_roles(toolkit: Path, tmp_path: Path) -> None:
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    assert _api_env(out)["IMAGE_EDIT_ENABLED"] == "false"
    worker, _ = _worker_env(out)
    assert worker["IMAGE_EDIT_ENABLED"] == "false"
    for env in (_api_env(out), worker):
        assert "LLM_MODEL_IMAGE_EDIT" not in env
        assert "IMAGE_EDIT_TENANT_DAILY_LIMIT" not in env


def test_image_edit_switch_and_limits_reach_both_roles(
    toolkit: Path, tmp_path: Path, clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_env(
        toolkit,
        "staging",
        **IMAGE_EDIT_ROUTER,
        LLM_MODEL_IMAGE_EDIT="google/gemini-3.1-flash-image",
        IMAGE_EDIT_TENANT_DAILY_LIMIT="7",
        IMAGE_EDIT_PLATFORM_DAILY_SHARE="0.4",
    )
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    api = _api_env(out)
    worker, secrets = _worker_env(out)
    for env in (api, worker):
        assert env["IMAGE_EDIT_ENABLED"] == "true"
        assert env["IMAGE_EDIT_TENANT_DAILY_LIMIT"] == "7"
        assert env["IMAGE_EDIT_PLATFORM_DAILY_SHARE"] == "0.4"
        # the api keys and labels candidates by the model the worker calls
        assert env["LLM_MODEL_IMAGE_EDIT"] == "google/gemini-3.1-flash-image"
    assert "OPENROUTER_API_KEY" in secrets
    _boot(monkeypatch, worker, secrets)
    deploy = next(line for line in out.splitlines() if line.startswith("+ gcloud run deploy "))
    _boot(monkeypatch, api, _secret_names(deploy))


@pytest.mark.parametrize("env_name", ["staging", "prod"])
def test_api_env_boots_with_image_edit_on(
    toolkit: Path, tmp_path: Path, clean_env: Path, monkeypatch: pytest.MonkeyPatch, env_name: str
) -> None:
    _set_env(toolkit, env_name, **IMAGE_EDIT_ROUTER)
    out = _run(toolkit, tmp_path, env_name, "deploy", "--dry-run", "--sha", SHA).stdout
    env = _api_env(out)
    assert env["IMAGE_EDIT_ENABLED"] == "true"
    deploy = next(line for line in out.splitlines() if line.startswith("+ gcloud run deploy "))
    _boot(monkeypatch, env, _secret_names(deploy))


def test_image_edit_on_without_the_gateway_refused(toolkit: Path, tmp_path: Path) -> None:
    _set_env(toolkit, "staging", IMAGE_EDIT_ENABLED="true")
    result = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA)
    assert result.returncode != 0
    assert "IMAGE_EDIT_ENABLED=true needs LLM_GATEWAY=openrouter" in result.stderr


def test_bad_image_edit_switch_refused(toolkit: Path, tmp_path: Path) -> None:
    _set_env(toolkit, "staging", IMAGE_EDIT_ENABLED="yes")
    result = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA)
    assert result.returncode != 0 and "IMAGE_EDIT_ENABLED must be true or false" in result.stderr


# ---- V1 hardening #2: the optional Slack webhook ----


@pytest.mark.parametrize("env_name", ["staging", "prod"])
def test_setup_prepares_the_optional_slack_webhook(
    toolkit: Path, tmp_path: Path, env_name: str
) -> None:
    """`setup` creates ALERT_SLACK_WEBHOOK and grants it to the worker, never sets a value and
    never stops for it; the api and the jobs never get it."""
    out = _run(toolkit, tmp_path, env_name, "setup", "--dry-run", "--sha", SHA).stdout
    assert "secrets create ALERT_SLACK_WEBHOOK " in out
    assert "ALERT_SLACK_WEBHOOK" in _secret_grants(out, "aimos-worker")
    assert "ALERT_SLACK_WEBHOOK" not in _secret_grants(out, "aimos-api")
    assert "ALERT_SLACK_WEBHOOK" not in _secret_grants(out, "aimos-maint")
    assert "ALERT_SLACK_WEBHOOK has no value (optional" in out
    assert "versions add ALERT_SLACK_WEBHOOK --data-file=- --project" in out  # the hint only


def test_worker_binds_the_slack_webhook_only_with_a_value(toolkit: Path, tmp_path: Path) -> None:
    """A dry run reads the webhook as empty: the worker deploys without it (an empty secret
    would fail the deploy) and says so."""
    result = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA)
    assert result.returncode == 0, result.stderr
    assert "ALERT_SLACK_WEBHOOK has no value: not bound (optional)" in result.stderr
    assert "ALERT_SLACK_WEBHOOK" not in "\n".join(
        x for x in result.stdout.splitlines() if "worker-pools" in x or "secretKeyRef" in x
    )


SI14_SWITCHES = (
    "CATEGORY_SUGGEST_ENABLED",
    "COLUMN_MAP_AI_ENABLED",
    "CATEGORY_LAMPS_ENABLED",
    "PHOTO_QA_ENABLED",
)


def test_si14_switches_are_off_in_both_env_files(repo_root: Path) -> None:
    """SI14 (D12): the four V1.1 assist switches are committed off, and no per-purpose gateway is
    set, so every model call keeps LLM_GATEWAY."""
    for env_name in ("staging", "prod"):
        text = (repo_root / DEPLOY / f"{env_name}.env").read_text(encoding="utf-8")
        values = dict(re.findall(r"^([A-Z_]+)=(.*)$", text, re.MULTILINE))
        for name in SI14_SWITCHES:
            assert values[name] == "false", (env_name, name)
        assert not [k for k in values if k.startswith("LLM_GATEWAY_")], env_name


def test_si14_switches_reach_both_roles(toolkit: Path, tmp_path: Path) -> None:
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    for name in SI14_SWITCHES:
        assert _api_env(out)[name] == "false", name
        assert _worker_container(out)[0][name] == "false", name
    _set_env(toolkit, "staging", CATEGORY_SUGGEST_ENABLED="true")
    out = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA).stdout
    assert _api_env(out)["CATEGORY_SUGGEST_ENABLED"] == "true"
    assert _worker_container(out)[0]["CATEGORY_SUGGEST_ENABLED"] == "true"
    _set_env(toolkit, "staging", PHOTO_QA_ENABLED="yes")
    result = _run(toolkit, tmp_path, "staging", "deploy", "--dry-run", "--sha", SHA)
    assert result.returncode != 0
    assert "PHOTO_QA_ENABLED must be true or false" in result.stderr
