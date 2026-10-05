"""Makefile host ports, validation and the compose-port guard (ADR-0011 amendment)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRUB = (
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_FILE",
    "PG_HOST_PORT",
    "MINIO_API_HOST_PORT",
    "MINIO_CONSOLE_HOST_PORT",
    "API_HOST_PORT",
    "MOCK_HOST_PORT",
    "TEST_DATABASE_URL",
    "ISO_DATABASE_URL",
    "DATABASE_URL",
    "ALEMBIC_DATABASE_URL",
    "S3_ENDPOINT_URL",
    "PORT",
    "CI",
    "MAKEFLAGS",
)
_PORT_KEYS = ("PG_HOST_PORT", "API_HOST_PORT", "MINIO_API_HOST_PORT", "MOCK_HOST_PORT")
_BAD_PORTS = (
    "abc",
    "0",
    "65536",
    "05433",
    "0.0.0.0:5433",
    "5433-5440",
    "5433 5434",
    "5433\n5434",
)
_PROBE = (
    "probe: ; @env | grep -E "
    "'^(COMPOSE_PROJECT_NAME|PG_HOST_PORT|MINIO_API_HOST_PORT|"
    "MINIO_CONSOLE_HOST_PORT|API_HOST_PORT|MOCK_HOST_PORT)=' || true\n"
)


@pytest.fixture
def sandbox(tmp_path: Path, repo_root: Path) -> Path:
    scripts = tmp_path / "scripts" / "ci"
    scripts.mkdir(parents=True)
    shutil.copy(repo_root / "Makefile", tmp_path / "Makefile")
    helper = scripts / "worktree_env.sh"
    shutil.copy(repo_root / "scripts" / "ci" / "worktree_env.sh", helper)
    helper.chmod(0o755)
    return tmp_path


def _child(extra: dict[str, str] | None = None, path_prefix: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for key in _SCRUB:
        env.pop(key, None)
    if path_prefix is not None:
        env["PATH"] = "{}{}{}".format(path_prefix, os.pathsep, env.get("PATH", ""))
    if extra:
        env.update(extra)
    return env


def _make(
    sandbox: Path,
    args: list[str],
    extra: dict[str, str] | None = None,
    path_prefix: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "-s", "-C", str(sandbox), *args],
        env=_child(extra, path_prefix),
        capture_output=True,
        text=True,
        check=False,
    )


def _probe(sandbox: Path, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    (sandbox / "probe.mk").write_text(_PROBE, encoding="utf-8")
    return subprocess.run(
        ["make", "-s", "-C", str(sandbox), "-f", "Makefile", "-f", "probe.mk", "probe"],
        env=_child(extra),
        capture_output=True,
        text=True,
        check=False,
    )


def test_defaults_without_file(sandbox: Path) -> None:
    api = _make(sandbox, ["-n", "api-tests"])
    assert api.returncode == 0, api.stderr
    assert "@localhost:5432/mos_test" in api.stdout
    iso = _make(sandbox, ["-n", "tenant-isolation"])
    assert iso.returncode == 0, iso.stderr
    assert "@localhost:5432/mos_iso" in iso.stdout
    dev = _make(sandbox, ["-n", "dev"])
    assert dev.returncode == 0, dev.stderr
    assert "--port 8000" in dev.stdout
    assert "--host" not in dev.stdout


def test_mock_port_default_and_override(sandbox: Path) -> None:
    # S3 D5: `make mock` binds 127.0.0.1 on MOCK_HOST_PORT (default 4010), per checkout.
    default = _make(sandbox, ["-n", "mock"])
    assert default.returncode == 0, default.stderr
    assert "--host 127.0.0.1 --port 4010" in default.stdout
    (sandbox / ".worktree.env").write_text("MOCK_HOST_PORT=4011\n", encoding="utf-8")
    override = _make(sandbox, ["-n", "mock"])
    assert override.returncode == 0, override.stderr
    assert "--host 127.0.0.1 --port 4011" in override.stdout
    assert _probe(sandbox).stdout.splitlines() == ["MOCK_HOST_PORT=4011"]


def test_file_overrides_urls_and_port(sandbox: Path) -> None:
    (sandbox / ".worktree.env").write_text(
        "PG_HOST_PORT=5433\nAPI_HOST_PORT=8001\n", encoding="utf-8"
    )
    api = _make(sandbox, ["-n", "api-tests"])
    assert api.returncode == 0, api.stderr
    assert ":5433/mos_test" in api.stdout
    iso = _make(sandbox, ["-n", "tenant-isolation"])
    assert ":5433/mos_iso" in iso.stdout
    dev = _make(sandbox, ["-n", "dev"])
    assert "--port 8001" in dev.stdout


def test_export_only_keys_the_file_sets(sandbox: Path) -> None:
    (sandbox / ".worktree.env").write_text("PG_HOST_PORT=5433\n", encoding="utf-8")
    probed = _probe(sandbox)
    assert probed.returncode == 0, probed.stderr
    assert probed.stdout.splitlines() == ["PG_HOST_PORT=5433"]
    (sandbox / ".worktree.env").unlink()
    empty = _probe(sandbox)
    assert empty.returncode == 0, empty.stderr
    assert empty.stdout == ""


def test_empty_value_means_default(sandbox: Path) -> None:
    (sandbox / ".worktree.env").write_text("PG_HOST_PORT=\n", encoding="utf-8")
    api = _make(sandbox, ["-n", "api-tests"])
    assert api.returncode == 0, api.stderr
    assert ":5432/mos_test" in api.stdout
    probed = _probe(sandbox)
    assert probed.stdout == ""


@pytest.mark.parametrize(
    "text",
    [
        "COV_FAIL_UNDER=0\n",
        "PG_HOST_PORT=abc\n",
        "SHELL=/bin/sh\n",
        "PG_HOST_PORT=5433\r\n",
        "# $(shell touch pwned)\n",
        "PG_HOST_PORT=5433\\\n",
    ],
)
def test_rejected_file_stops_every_target(sandbox: Path, text: str) -> None:
    (sandbox / ".worktree.env").write_text(text, encoding="utf-8")
    for target in ("help", "api-tests"):
        result = _make(sandbox, ["-n", target])
        assert result.returncode != 0, text
        assert "uv run" not in result.stdout
        assert "pytest" not in result.stdout
        assert "make dev |" not in result.stdout
        if "COV_FAIL_UNDER=0" in text:
            assert "--cov-fail-under=0" not in result.stdout + result.stderr
    assert not (sandbox / "pwned").exists()


def test_silent_failing_helper_stops_make(sandbox: Path) -> None:
    helper = sandbox / "scripts" / "ci" / "worktree_env.sh"
    helper.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    helper.chmod(0o755)
    (sandbox / ".worktree.env").write_text("PG_HOST_PORT=5433\n", encoding="utf-8")
    result = _make(sandbox, ["-n", "help"])
    assert result.returncode != 0


def test_missing_helper_stops_make(sandbox: Path) -> None:
    (sandbox / ".worktree.env").write_text("PG_HOST_PORT=5433\n", encoding="utf-8")
    (sandbox / "scripts" / "ci" / "worktree_env.sh").unlink()
    result = _make(sandbox, ["-n", "help"])
    assert result.returncode != 0


def test_command_line_wins(sandbox: Path) -> None:
    (sandbox / ".worktree.env").write_text("PG_HOST_PORT=5433\n", encoding="utf-8")
    result = _make(sandbox, ["-n", "api-tests", "PG_HOST_PORT=5440"])
    assert result.returncode == 0, result.stderr
    assert ":5440/" in result.stdout


def test_explicit_test_url_still_wins(sandbox: Path) -> None:
    url = "postgresql+psycopg://postgres:postgres@localhost:6000/mos_test"
    result = _make(sandbox, ["-n", "api-tests"], {"TEST_DATABASE_URL": url})
    assert result.returncode == 0, result.stderr
    assert url in result.stdout


def test_env_var_without_file(sandbox: Path) -> None:
    result = _make(sandbox, ["-n", "api-tests"], {"PG_HOST_PORT": "5433"})
    assert result.returncode == 0, result.stderr
    assert ":5433/mos_test" in result.stdout


@pytest.mark.parametrize("source", ["env", "command", "file"])
@pytest.mark.parametrize("key", _PORT_KEYS)
@pytest.mark.parametrize("value", _BAD_PORTS)
def test_effective_ports_validated_at_parse_time(
    sandbox: Path, source: str, key: str, value: str
) -> None:
    if source == "file" and "\n" in value:
        pytest.skip("helper splits the line before make's port check")
    extra = None
    args = ["-n", "help"]
    if source == "env":
        extra = {key: value}
    elif source == "command":
        args.append(f"{key}={value}")
    else:
        (sandbox / ".worktree.env").write_text(f"{key}={value}\n", encoding="utf-8")
    result = _make(sandbox, args, extra)
    assert result.returncode != 0
    assert key in result.stderr


def test_compose_port_guard_in_db_targets(sandbox: Path) -> None:
    (sandbox / ".worktree.env").write_text("PG_HOST_PORT=5433\n", encoding="utf-8")
    markers = {"test-db": "psql", "api-tests": "pytest", "tenant-isolation": "pytest"}
    for target, marker in markers.items():
        result = _make(sandbox, ["-n", target])
        assert result.returncode == 0, result.stderr
        guard = result.stdout.index("docker compose port postgres 5432")
        assert "127.0.0.1:5433" in result.stdout
        assert guard < result.stdout.index(marker)
    for target in ("api-tests", "tenant-isolation"):
        quiet = _make(sandbox, ["-n", target], {"CI": "true"})
        assert quiet.returncode == 0, quiet.stderr
        assert "docker compose port postgres 5432" not in quiet.stdout


def _install_fakes(sandbox: Path) -> Path:
    bindir = sandbox / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$FAKE_PG_PUBLISHED\"\n", encoding="utf-8")
    docker.chmod(0o755)
    uv = bindir / "uv"
    uv.write_text("#!/bin/sh\ntouch uv-ran\nexit 0\n", encoding="utf-8")
    uv.chmod(0o755)
    return bindir


def test_compose_port_guard_blocks_wrong_stack(sandbox: Path) -> None:
    (sandbox / ".worktree.env").write_text("PG_HOST_PORT=5433\n", encoding="utf-8")
    bindir = _install_fakes(sandbox)
    wrong = _make(
        sandbox,
        ["api-tests"],
        {"FAKE_PG_PUBLISHED": "127.0.0.1:5432"},
        bindir,
    )
    assert wrong.returncode != 0
    assert "want 127.0.0.1:5433" in wrong.stderr
    assert not (sandbox / "uv-ran").exists()
    right = _make(
        sandbox,
        ["api-tests"],
        {"FAKE_PG_PUBLISHED": "127.0.0.1:5433"},
        bindir,
    )
    assert right.returncode == 0, right.stderr
    assert (sandbox / "uv-ran").is_file()
    (sandbox / "uv-ran").unlink()
    empty = _make(sandbox, ["api-tests"], {"FAKE_PG_PUBLISHED": ""}, bindir)
    assert empty.returncode != 0
    assert not (sandbox / "uv-ran").exists()


def test_dev_runs_check_before_compose(repo_root: Path) -> None:
    text = (repo_root / "Makefile").read_text(encoding="utf-8")
    recipe = re.search(r"^dev:\n((?:\t.*\n)+)", text, re.MULTILINE)
    assert recipe is not None
    body = recipe.group(1)
    assert body.index("scripts/ci/worktree_env.sh check") < body.index("docker compose")
