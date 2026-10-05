"""S3 T22 (AC15): scripts/ci/publish_schemas.sh refuses every unsafe release, in order.

Each test builds a throwaway git repo holding the script and the files it reads, with stub `make`
and `pnpm` on PATH that only record their arguments. No network, no Docker.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path("scripts/ci/publish_schemas.sh")
IDENT = [
    "-c",
    "user.name=t",
    "-c",
    "user.email=t@example.invalid",
    "-c",
    "core.hooksPath=/dev/null",
]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *IDENT, *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write_release(repo: Path, version: str, heading: str) -> None:
    ts = repo / "packages/schemas/ts"
    ts.mkdir(parents=True, exist_ok=True)
    (ts / "package.json").write_text(json.dumps({"version": version}), "utf-8")
    js = repo / "packages/schemas/jsonschema"
    js.mkdir(parents=True, exist_ok=True)
    (js / "openapi.json").write_text(json.dumps({"info": {"version": version}}), "utf-8")
    (repo / "packages/schemas/CHANGELOG.md").write_text(f"# x\n\n{heading}\n\n- y\n", "utf-8")


@pytest.fixture
def repo(tmp_path: Path, repo_root: Path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts/ci").mkdir(parents=True)
    shutil.copy(repo_root / SCRIPT, root / SCRIPT)
    _git(root, "init", "-q", "-b", "main")
    _write_release(root, "1.0.0", "## 1.0.0 (2026-10-20)")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "release")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


@pytest.fixture
def stubs(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("make", "pnpm"):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >>"{tmp_path}/calls.log"\n', "utf-8")
        stub.chmod(0o755)
    return bin_dir


def _run(repo: Path, stubs: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    full = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
    full["PATH"] = f"{stubs}{os.pathsep}{full['PATH']}"
    full.update(
        {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REF_TYPE": "tag",
            "GITHUB_REF_NAME": "schemas-v1.0.0",
            "GITHUB_SHA": _git(repo, "rev-parse", "HEAD"),
            "NODE_AUTH_TOKEN": "token-value-never-on-disk",
        }
    )
    full.update(env)
    result = subprocess.run(
        ["bash", str(SCRIPT)], cwd=repo, env=full, capture_output=True, text=True, check=False
    )
    log = stubs.parent / "calls.log"
    calls = log.read_text("utf-8").splitlines() if log.exists() else []
    return result, calls


def test_publish_happy_path(repo: Path, stubs: Path) -> None:
    result, calls = _run(repo, stubs)
    assert result.returncode == 0, result.stderr
    assert calls == [
        "make -C packages/schemas all",
        "make -C packages/schemas ts-consumer-check",
        "pnpm publish --no-git-checks",
    ]
    assert "token-value-never-on-disk" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "env",
    [
        {"GITHUB_ACTIONS": ""},
        {"GITHUB_REF_TYPE": "branch"},
        {"GITHUB_REF_NAME": "v1.0.0"},
    ],
)
def test_publish_refuses_outside_tag_ci(repo: Path, stubs: Path, env: dict[str, str]) -> None:
    result, calls = _run(repo, stubs, **env)
    assert result.returncode == 1
    assert result.stderr.startswith("publish: ")
    assert calls == []


def test_publish_refuses_commit_not_on_main(repo: Path, stubs: Path) -> None:
    _git(repo, "switch", "-q", "-c", "feature")
    (repo / "extra.txt").write_text("x\n", "utf-8")
    _git(repo, "add", "extra.txt")
    _git(repo, "commit", "-q", "-m", "branch only")
    result, calls = _run(repo, stubs, GITHUB_SHA=_git(repo, "rev-parse", "HEAD"))
    assert result.returncode == 1
    assert "publish: tag commit is not on main" in result.stderr
    assert not [c for c in calls if c.startswith("pnpm")]
    assert calls == []


def test_publish_refuses_version_mismatch(repo: Path, stubs: Path) -> None:
    result, calls = _run(repo, stubs, GITHUB_REF_NAME="schemas-v1.1.0")
    assert result.returncode == 1
    assert "version mismatch: tag 1.1.0, package.json 1.0.0, openapi.json 1.0.0" in result.stderr
    assert not [c for c in calls if c.startswith("pnpm")]


def test_publish_refuses_unreleased_heading(repo: Path, stubs: Path) -> None:
    _write_release(repo, "1.0.0", "## 1.0.0 (unreleased)")
    _git(repo, "commit", "-q", "-am", "reopen")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    result, calls = _run(repo, stubs)
    assert result.returncode == 1
    assert "CHANGELOG heading for 1.0.0 is still (unreleased)" in result.stderr
    assert not [c for c in calls if c.startswith("pnpm")]


def test_publish_refuses_without_token(repo: Path, stubs: Path) -> None:
    result, calls = _run(repo, stubs, NODE_AUTH_TOKEN="")
    assert result.returncode == 1
    assert "NODE_AUTH_TOKEN is not set" in result.stderr
    assert not [c for c in calls if c.startswith("pnpm")]


def test_publish_steps_ordered(repo_root: Path) -> None:
    text = (repo_root / SCRIPT).read_text("utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    order = [
        code.index('[ "${GITHUB_ACTIONS:-}" = "true" ]'),
        code.index("git merge-base --is-ancestor"),
        code.index("make -C packages/schemas all"),
        code.index("version mismatch"),
        code.index("(unreleased)"),
        code.index("make -C packages/schemas ts-consumer-check"),
        code.index("pnpm publish"),
    ]
    assert order == sorted(order)
    assert "_authToken=${NODE_AUTH_TOKEN}" in text
