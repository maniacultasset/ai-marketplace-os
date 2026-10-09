"""scripts/ci/worktree_env.sh: exports and check (ADR-0011 amendment)."""

from __future__ import annotations

import os
import re
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
_EXPORT_LINE = re.compile(r"^export [A-Z_]+=[a-z0-9_-]+$")


def _env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _SCRUB:
        env.pop(key, None)
    return env


def _run(
    repo_root: Path,
    tmp_path: Path,
    args: list[str],
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    script = repo_root / "scripts" / "ci" / "worktree_env.sh"
    merged = _env()
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=tmp_path,
        env=merged,
        capture_output=True,
        text=True,
        check=False,
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, env=_env(), check=True, capture_output=True)


def test_missing_file_prints_nothing_exit_0(repo_root: Path, tmp_path: Path) -> None:
    result = _run(repo_root, tmp_path, ["exports"])
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_valid_file_prints_exports(repo_root: Path, tmp_path: Path) -> None:
    text = (
        "COMPOSE_PROJECT_NAME=trackb\n"
        "PG_HOST_PORT=5433\n"
        "MINIO_API_HOST_PORT=9010\n"
        "MINIO_CONSOLE_HOST_PORT=9011\n"
        "API_HOST_PORT=8001\n"
        "MOCK_HOST_PORT=4011\n"
    )
    (tmp_path / ".worktree.env").write_text(text, encoding="utf-8")
    result = _run(repo_root, tmp_path, ["exports"])
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "export COMPOSE_PROJECT_NAME=trackb",
        "export PG_HOST_PORT=5433",
        "export MINIO_API_HOST_PORT=9010",
        "export MINIO_CONSOLE_HOST_PORT=9011",
        "export API_HOST_PORT=8001",
        "export MOCK_HOST_PORT=4011",
    ]


def test_comments_and_blank_lines_ok(repo_root: Path, tmp_path: Path) -> None:
    (tmp_path / ".worktree.env").write_text("\n# note\n\nPG_HOST_PORT=5433\n", encoding="utf-8")
    result = _run(repo_root, tmp_path, ["exports"])
    assert result.returncode == 0, result.stderr
    assert result.stdout == "export PG_HOST_PORT=5433\n"


def test_empty_value_is_not_exported(repo_root: Path, tmp_path: Path) -> None:
    (tmp_path / ".worktree.env").write_text("PG_HOST_PORT=\n", encoding="utf-8")
    result = _run(repo_root, tmp_path, ["exports"])
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "text",
    [
        "COV_FAIL_UNDER=0\n",
        "SHELL=/bin/sh\n",
        "export PG_HOST_PORT=5433\n",
        'PG_HOST_PORT="5433"\n',
        "PG_HOST_PORT = 5433\n",
        "PG_HOST_PORT=5433 \n",
        "PG_HOST_PORT=5433\r\n",
        "PG_HOST_PORT=abc\n",
        "PG_HOST_PORT=0\n",
        "PG_HOST_PORT=65536\n",
        "PG_HOST_PORT=05433\n",
        "PG_HOST_PORT=-1\n",
        "PG_HOST_PORT=0.0.0.0:5433\n",
        "PG_HOST_PORT=5433-5440\n",
        "PG_HOST_PORT=5433\nPG_HOST_PORT=5434\n",
        "COMPOSE_PROJECT_NAME=Track_B\n",
        "COMPOSE_PROJECT_NAME=-x\n",
        "COMPOSE_PROJECT_NAME=a b\n",
        "PG_HOST_PORT=$(id)\n",
        "PG_HOST_PORT=5433;touch pwned\n",
        "# costs $5\n",
        "# ends in \\\n",
        "PG_HOST_PORT=5433\\\n",
    ],
)
def test_rejects(repo_root: Path, tmp_path: Path, text: str) -> None:
    (tmp_path / ".worktree.env").write_text(text, encoding="utf-8")
    result = _run(repo_root, tmp_path, ["exports"])
    assert result.returncode == 1
    assert result.stdout == ""
    assert re.search(r":\d+:", result.stderr)
    assert not (tmp_path / "pwned").exists()


def test_boundaries_accepted(repo_root: Path, tmp_path: Path) -> None:
    name = "a" + ("b" * 99)
    body = f"PG_HOST_PORT=1\nMINIO_API_HOST_PORT=65535\nCOMPOSE_PROJECT_NAME={name}"
    (tmp_path / ".worktree.env").write_bytes(body.encode("utf-8"))
    result = _run(repo_root, tmp_path, ["exports"])
    assert result.returncode == 0, result.stderr
    assert "export PG_HOST_PORT=1" in result.stdout
    assert "export MINIO_API_HOST_PORT=65535" in result.stdout
    assert f"export COMPOSE_PROJECT_NAME={name}" in result.stdout


def test_directory_in_place_of_file_rejected(repo_root: Path, tmp_path: Path) -> None:
    (tmp_path / ".worktree.env").mkdir()
    result = _run(repo_root, tmp_path, ["exports"])
    assert result.returncode == 1
    assert result.stdout == ""


def test_output_is_eval_safe(repo_root: Path, tmp_path: Path) -> None:
    (tmp_path / ".worktree.env").write_text(
        "COMPOSE_PROJECT_NAME=a-b_c\nPG_HOST_PORT=5433\n", encoding="utf-8"
    )
    result = _run(repo_root, tmp_path, ["exports"])
    assert result.returncode == 0, result.stderr
    for line in result.stdout.splitlines():
        assert _EXPORT_LINE.fullmatch(line), line


def _example(repo_root: Path) -> str:
    return (repo_root / ".env.example").read_text(encoding="utf-8")


def _ports_env(example: str) -> str:
    text = example.replace("localhost:5432", "localhost:5433")
    text = text.replace("localhost:9000", "localhost:9010")
    return text.replace("PORT=8000", "PORT=8001")


def test_check_defaults_with_example_env(repo_root: Path, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(_example(repo_root), encoding="utf-8")
    result = _run(repo_root, tmp_path, ["check"])
    assert result.returncode == 0, result.stderr


def test_check_matching_worktree(repo_root: Path, tmp_path: Path) -> None:
    (tmp_path / ".worktree.env").write_text(
        "PG_HOST_PORT=5433\nMINIO_API_HOST_PORT=9010\n"
        "MINIO_CONSOLE_HOST_PORT=9011\nAPI_HOST_PORT=8001\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(_ports_env(_example(repo_root)), encoding="utf-8")
    result = _run(repo_root, tmp_path, ["check"])
    assert result.returncode == 0, result.stderr


def test_check_dotenv_forms_accepted(repo_root: Path, tmp_path: Path) -> None:
    (tmp_path / ".worktree.env").write_text(
        "PG_HOST_PORT=5433\nMINIO_API_HOST_PORT=9010\nAPI_HOST_PORT=8001\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        'export DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5433/marketplace_os"\n'
        'export ALEMBIC_DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5433/marketplace_os"\n'
        "S3_ENDPOINT_URL='http://localhost:9010'\n"
        "PORT=8001\n",
        encoding="utf-8",
    )
    result = _run(repo_root, tmp_path, ["check"])
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("dotenv", "snippet"),
    [
        (
            "DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os\n",
            "DATABASE_URL port 5432 != PG_HOST_PORT 5433",
        ),
        (
            "ALEMBIC_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os\n",
            "ALEMBIC_DATABASE_URL port 5432 != PG_HOST_PORT 5433",
        ),
        (
            "S3_ENDPOINT_URL=http://localhost:9000\n",
            "S3_ENDPOINT_URL port 9000 != MINIO_API_HOST_PORT 9010",
        ),
        ("PORT=8000\n", "PORT port 8000 != API_HOST_PORT 8001"),
        (
            'export DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os"\n',
            "DATABASE_URL port 5432 != PG_HOST_PORT 5433",
        ),
        (
            "DATABASE_URL='postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os'\n",
            "DATABASE_URL port 5432 != PG_HOST_PORT 5433",
        ),
        (
            "DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost/marketplace_os\n",
            "DATABASE_URL has no port",
        ),
        ("PORT=abc\n", "PORT"),
    ],
)
def test_check_mismatch(repo_root: Path, tmp_path: Path, dotenv: str, snippet: str) -> None:
    (tmp_path / ".worktree.env").write_text(
        "PG_HOST_PORT=5433\nMINIO_API_HOST_PORT=9010\nAPI_HOST_PORT=8001\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(dotenv, encoding="utf-8")
    result = _run(repo_root, tmp_path, ["check"])
    assert result.returncode == 1
    assert snippet in result.stderr


def test_check_env_var_beats_dotenv(repo_root: Path, tmp_path: Path) -> None:
    (tmp_path / ".worktree.env").write_text("PG_HOST_PORT=5433\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os\n",
        encoding="utf-8",
    )
    result = _run(
        repo_root,
        tmp_path,
        ["check"],
        {"DATABASE_URL": "postgresql+psycopg://postgres:postgres@localhost:5433/marketplace_os"},
    )
    assert result.returncode == 0, result.stderr


def test_check_file_beats_env_var(repo_root: Path, tmp_path: Path) -> None:
    (tmp_path / ".worktree.env").write_text("PG_HOST_PORT=5433\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5433/marketplace_os\n",
        encoding="utf-8",
    )
    ok = _run(repo_root, tmp_path, ["check"], {"PG_HOST_PORT": "5440"})
    assert ok.returncode == 0, ok.stderr
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5440/marketplace_os\n",
        encoding="utf-8",
    )
    bad = _run(repo_root, tmp_path, ["check"], {"PG_HOST_PORT": "5440"})
    assert bad.returncode == 1


def test_check_missing_dotenv_skips_absent_keys(repo_root: Path, tmp_path: Path) -> None:
    result = _run(repo_root, tmp_path, ["check"])
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("API_HOST_PORT", "abc"),
        ("MOCK_HOST_PORT", "abc"),
        ("MOCK_HOST_PORT", "65536"),
        ("PG_HOST_PORT", "0.0.0.0:5433"),
        ("PG_HOST_PORT", "5433-5440"),
        ("PG_HOST_PORT", "5433\n"),
    ],
)
def test_check_bad_env_ports_rejected(
    repo_root: Path, tmp_path: Path, key: str, value: str
) -> None:
    result = _run(repo_root, tmp_path, ["check"], {key: value})
    assert result.returncode == 1
    assert key in result.stderr


def test_check_rejected_file_fails(repo_root: Path, tmp_path: Path) -> None:
    (tmp_path / ".worktree.env").write_text("PG_HOST_PORT=abc\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os\n",
        encoding="utf-8",
    )
    result = _run(repo_root, tmp_path, ["check"])
    assert result.returncode == 1
    assert "DATABASE_URL port" not in result.stderr


def test_check_never_leaks_or_sources_dotenv(repo_root: Path, tmp_path: Path) -> None:
    (tmp_path / ".worktree.env").write_text("PG_HOST_PORT=5433\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql+psycopg://postgres:s3cr3t-dummy@localhost:5432/x\n"
        "JUNK=$(touch pwned)\n"
        "JUNK2=`touch pwned2`\n",
        encoding="utf-8",
    )
    result = _run(repo_root, tmp_path, ["check"])
    assert result.returncode == 1
    assert "s3cr3t-dummy" not in result.stderr
    assert "postgres:" not in result.stderr
    assert not (tmp_path / "pwned").exists()
    assert not (tmp_path / "pwned2").exists()
    helper = (repo_root / "scripts" / "ci" / "worktree_env.sh").read_text(encoding="utf-8")
    assert "urlsplit(sys.argv" not in helper
    assert "source " not in helper
    assert ". ./.env" not in helper


@pytest.mark.parametrize(
    ("where", "own", "primary_file", "code"),
    [
        ("wt", None, None, 0),
        ("wt", "COMPOSE_PROJECT_NAME=primary\n", None, 1),
        ("wt", "COMPOSE_PROJECT_NAME=shared\n", "COMPOSE_PROJECT_NAME=shared\n", 1),
        ("dot", None, None, 1),
        ("primary", None, None, 0),
        ("primary", "COMPOSE_PROJECT_NAME=primary\n", None, 0),
        ("primary", "COMPOSE_PROJECT_NAME=shared\n", None, 0),
    ],
)
def test_check_linked_worktree_project(
    repo_root: Path,
    tmp_path: Path,
    where: str,
    own: str | None,
    primary_file: str | None,
    code: int,
) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q")
    _git(
        primary,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        "init",
    )
    wt = tmp_path / "primary-wt"
    dotted = tmp_path / "pri.mary"
    _git(primary, "worktree", "add", "--detach", "-q", str(wt), "HEAD")
    _git(primary, "worktree", "add", "--detach", "-q", str(dotted), "HEAD")
    homes = {"wt": wt, "dot": dotted, "primary": primary}
    cwd = homes[where]
    if own:
        (cwd / ".worktree.env").write_text(own, encoding="utf-8")
    if primary_file:
        (primary / ".worktree.env").write_text(primary_file, encoding="utf-8")
    result = _run(repo_root, cwd, ["check"])
    assert result.returncode == code, result.stderr
    if code == 1:
        assert "is the primary checkout's project" in result.stderr


def test_check_outside_git_skips_project_check(repo_root: Path, tmp_path: Path) -> None:
    result = _run(repo_root, tmp_path, ["check"])
    assert result.returncode == 0, result.stderr


def test_unknown_mode_exits_2(repo_root: Path, tmp_path: Path) -> None:
    result = _run(repo_root, tmp_path, ["image-tag"])
    assert result.returncode == 2
    assert result.stderr


def test_no_mode_exits_2(repo_root: Path, tmp_path: Path) -> None:
    result = _run(repo_root, tmp_path, [])
    assert result.returncode == 2
    assert result.stderr


def test_fake_marketplace_port_key(repo_root: Path, tmp_path: Path) -> None:
    """S15: the fake marketplace port is a per-checkout key (default 4020), validated like the
    others."""
    (tmp_path / ".worktree.env").write_text("FAKE_MARKETPLACE_HOST_PORT=4021\n", encoding="utf-8")
    result = _run(repo_root, tmp_path, ["exports", ".worktree.env"])
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["export FAKE_MARKETPLACE_HOST_PORT=4021"]
    (tmp_path / ".worktree.env").write_text("FAKE_MARKETPLACE_HOST_PORT=70000\n", encoding="utf-8")
    bad = _run(repo_root, tmp_path, ["exports", ".worktree.env"])
    assert bad.returncode != 0
    assert "FAKE_MARKETPLACE_HOST_PORT is not a valid port" in bad.stderr
    script = (repo_root / "scripts/ci/worktree_env.sh").read_text(encoding="utf-8")
    assert "FAKE_MARKETPLACE_HOST_PORT) printf '%s' 4020 ;;" in script
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert "FAKE_MARKETPLACE_HOST_PORT := $(or $(FAKE_MARKETPLACE_HOST_PORT),4020)" in makefile
    keys = next(line for line in makefile.splitlines() if line.startswith("WORKTREE_KEYS :="))
    assert "FAKE_MARKETPLACE_HOST_PORT" in keys.split()


def test_flag_off_api_port_key(repo_root: Path, tmp_path: Path) -> None:
    """S22 C2: the flag-off e2e API port is a per-checkout key (default 8011), validated like the
    others."""
    (tmp_path / ".worktree.env").write_text("FLAG_OFF_API_HOST_PORT=8232\n", encoding="utf-8")
    result = _run(repo_root, tmp_path, ["exports", ".worktree.env"])
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["export FLAG_OFF_API_HOST_PORT=8232"]
    (tmp_path / ".worktree.env").write_text("FLAG_OFF_API_HOST_PORT=0\n", encoding="utf-8")
    bad = _run(repo_root, tmp_path, ["exports", ".worktree.env"])
    assert bad.returncode != 0 and "FLAG_OFF_API_HOST_PORT is not a valid port" in bad.stderr
    script = (repo_root / "scripts/ci/worktree_env.sh").read_text(encoding="utf-8")
    assert "FLAG_OFF_API_HOST_PORT) printf '%s' 8011 ;;" in script
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert "FLAG_OFF_API_HOST_PORT := $(or $(FLAG_OFF_API_HOST_PORT),8011)" in makefile
    keys = next(line for line in makefile.splitlines() if line.startswith("WORKTREE_KEYS :="))
    assert "FLAG_OFF_API_HOST_PORT" in keys.split()
