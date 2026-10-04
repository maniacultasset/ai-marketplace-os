"""AC16: pre-commit runs only local hooks, and installs the commit-msg hook."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def config(repo_root: Path, load_yaml: Callable[[Path], Any]) -> dict[str, Any]:
    data = load_yaml(repo_root / ".pre-commit-config.yaml")
    assert isinstance(data, dict)
    return data


def test_all_hooks_local_and_commit_msg_installed(config: dict[str, Any]) -> None:
    assert config["default_install_hook_types"] == ["pre-commit", "commit-msg"]
    assert config["default_stages"] == ["pre-commit"]
    assert [repo["repo"] for repo in config["repos"]] == ["local"]
    hooks = {hook["id"]: hook for repo in config["repos"] for hook in repo["hooks"]}
    assert set(hooks) == {
        "ruff",
        "black",
        "mypy",
        "gitleaks",
        "commit-lint",
        "commit-attribution",
    }
    assert all(hook["language"] == "system" for hook in hooks.values())
    assert hooks["commit-lint"]["stages"] == ["commit-msg"]
    assert hooks["commit-lint"]["entry"].endswith("scripts/ci/commit_lint.py --message-file")
    assert hooks["commit-attribution"]["stages"] == ["commit-msg"]
    assert hooks["commit-attribution"]["entry"].endswith("scripts/ci/commit_attribution.py")
    assert "--staged" in hooks["gitleaks"]["entry"]
    assert "--force-exclude" in hooks["ruff"]["entry"]


def test_excludes_doc_tooling_and_scaffold(config: dict[str, Any]) -> None:
    exclude = re.compile(config["exclude"])
    for path in ("scaffold/a.py", "scripts/docgen/a.py", "scripts/trace/a.py", "docs/a.md"):
        assert exclude.search(path), path
    for path in ("apps/api/app/main.py", "scripts/ci/a.py", "docsx/a.py"):
        assert not exclude.search(path), path
