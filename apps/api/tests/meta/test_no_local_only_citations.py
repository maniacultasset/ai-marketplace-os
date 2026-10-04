"""Committed code under apps/packages/scripts/ci/.github must not cite local-only plan files."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_CITATION = re.compile(
    r"DEVIATIONS\.md|CODE-PLAN\.md|REPORT\.md|LOOP\.md|docs/plans/[A-Za-z0-9_.-]+/"
)
_ROOTS = ("apps", "packages", "scripts/ci", ".github")


def local_only_citations(root: Path, exclude: Path) -> list[str]:
    listed = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "--", *_ROOTS],
        text=True,
    )
    hits: list[str] = []
    exclude_resolved = exclude.resolve()
    for raw in listed.splitlines():
        path = root / raw
        if path.resolve() == exclude_resolved:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for number, line in enumerate(text.splitlines(), start=1):
            if _CITATION.search(line):
                hits.append(f"{raw}:{number}")
    return hits


@pytest.mark.parametrize(
    "form",
    [
        "see DEVIATIONS.md",
        "see CODE-PLAN.md",
        "see REPORT.md",
        "see LOOP.md",
        "see docs/plans/s1/",
    ],
)
def test_detector_flags_seeded_mentions(tmp_path: Path, form: str) -> None:
    repo = tmp_path / "seed"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    exclude = repo / "apps" / "skip.py"
    for root in _ROOTS:
        (repo / root).mkdir(parents=True, exist_ok=True)
        target = repo / root / "seed.txt"
        target.write_text(form + "\n", encoding="utf-8")
        subprocess.run(["git", "add", str(target.relative_to(repo))], cwd=repo, check=True)
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "docs" / "plans" / "README.md").write_text("see DEVIATIONS.md\n", encoding="utf-8")
    (repo / "README.md").write_text("see CODE-PLAN.md\n", encoding="utf-8")
    (repo / "MY-REPORT.txt").write_text("REPORT.md\n", encoding="utf-8")
    (repo / "outside.txt").write_text("LOOP.md\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "docs/plans/README.md", "README.md", "MY-REPORT.txt", "outside.txt"],
        cwd=repo,
        check=True,
    )
    untracked = repo / "apps" / "untracked.py"
    untracked.write_text("DEVIATIONS.md\n", encoding="utf-8")
    exclude.write_text("CODE-PLAN.md\n", encoding="utf-8")
    hits = local_only_citations(repo, exclude)
    assert len(hits) == 4
    assert all(h.endswith(":1") for h in hits)
    assert {h.split(":")[0] for h in hits} == {
        "apps/seed.txt",
        "packages/seed.txt",
        "scripts/ci/seed.txt",
        ".github/seed.txt",
    }


def test_committed_code_cites_no_local_only_files(repo_root: Path) -> None:
    exclude = Path(__file__)
    assert local_only_citations(repo_root, exclude) == []
