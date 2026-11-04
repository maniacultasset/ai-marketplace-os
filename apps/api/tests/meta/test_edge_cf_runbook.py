"""EDGE-CF fix round 1 (F1, F2): the owner-run steps in docs/runbooks/EDGE-CLOUDFLARE.md never
store a trailing newline in EDGE_ORIGIN_SECRET, never hand the Worker the two-value rotation
version, and check the edge after every Worker or secret change."""

from __future__ import annotations

import re
from pathlib import Path

DOC = "docs/runbooks/EDGE-CLOUDFLARE.md"
STATUS = "deploy.sh prod edge status"
SERVING = '"edge serving: yes"'


def _section(repo_root: Path, heading: str) -> str:
    text = (repo_root / DOC).read_text(encoding="utf-8")
    start = text.index(f"## {heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else None]


def _steps(section: str) -> dict[int, str]:
    """Numbered list items, keyed by number, with their continuation lines."""
    parts = re.split(r"^(\d+)\. ", section, flags=re.MULTILINE)
    return {int(parts[i]): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def test_secret_versions_are_added_without_a_trailing_newline(repo_root: Path) -> None:
    rotation = _section(repo_root, "Rotation")
    adds = [line for line in rotation.splitlines() if "gcloud secrets versions add" in line]
    assert len(adds) == 2
    for line in adds:
        assert "printf '%s" in line and "--data-file=-" in line, line
    assert not re.search(r"\becho\b[^`]*\|", rotation)


def test_worker_never_gets_the_two_value_version(repo_root: Path) -> None:
    worker = _steps(_section(repo_root, "Worker deploy (owner, dashboard)"))[3]
    access = next(line for line in worker.splitlines() if "versions access" in line)
    assert "| cut -d' ' -f1 | tr -d '\\n' | pbcopy" in access
    rotation = _steps(_section(repo_root, "Rotation"))
    assert "printf '%s' \"$NEW\" | pbcopy" in rotation[2]
    assert "versions access" not in rotation[2]


def test_edge_is_checked_after_every_worker_or_secret_change(repo_root: Path) -> None:
    worker = _steps(_section(repo_root, "Worker deploy (owner, dashboard)"))[3]
    rotation = _steps(_section(repo_root, "Rotation"))
    for step in (worker, rotation[2], rotation[3]):
        flat = " ".join(step.split())
        assert STATUS in flat and SERVING in flat, flat
