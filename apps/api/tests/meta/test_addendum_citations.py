"""Tracked `CANON-ADDENDUM.md:<n>` and `A§<k>:<n>` citations point at text (AGENTS.md §7 truth
rules; RULES.md §12): the cited line exists and is not blank, and an `A§<k>:<n>` line sits inside
section k. An edit that moves the addendum's lines turns every stale citation red here."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

ADDENDUM = "docs/canon/CANON-ADDENDUM.md"
_PLAIN = re.compile(r"CANON-ADDENDUM\.md:(\d+)")
_SECTION = re.compile(r"A§(\d+)[a-z]?:(\d+)")
_HEADING = re.compile(r"^## (\d+)\. ")
# docs/canon/CANON.md:13 cites A§2, A§7, A§8 and A§12 by their pre-S5 lines. Canon edits need a
# recorded approval row, so it stays a known failure (strict xfail below) until one is recorded.
_PENDING_CANON_EDIT = "docs/canon/CANON.md"
_SELF = "apps/api/tests/meta/test_addendum_citations.py"  # its fixtures cite on purpose


def _sections(addendum: list[str]) -> dict[int, range]:
    starts = [
        (int(m.group(1)), n) for n, line in enumerate(addendum, 1) if (m := _HEADING.match(line))
    ]
    ends = [n for n, line in enumerate(addendum, 1) if line.startswith("## ")]
    spans: dict[int, range] = {}
    for number, start in starts:
        end = next((e for e in ends if e > start), len(addendum) + 1)
        spans[number] = range(start, end)
    return spans


def citation_errors(
    paths: Iterable[str], read: Callable[[str], str], addendum_text: str
) -> list[str]:
    addendum = addendum_text.splitlines()
    spans = _sections(addendum)

    def blank(n: int) -> bool:
        return not 1 <= n <= len(addendum) or not addendum[n - 1].strip()

    errors: list[str] = []
    for path in paths:
        for number, line in enumerate(read(path).splitlines(), start=1):
            for m in _PLAIN.finditer(line):
                if blank(int(m.group(1))):
                    errors.append(f"{path}:{number}: {m.group(0)} is blank or missing")
            for m in _SECTION.finditer(line):
                section, target = int(m.group(1)), int(m.group(2))
                if blank(target) or target not in spans.get(section, range(0)):
                    errors.append(f"{path}:{number}: {m.group(0)} is not a line of A§{section}")
    return errors


def _tracked(root: Path) -> list[str]:
    listed = subprocess.check_output(["git", "-C", str(root), "ls-files"], text=True)
    return [
        p
        for p in listed.splitlines()
        if p not in (ADDENDUM, _SELF) and not p.startswith("docs/archive/") and (root / p).is_file()
    ]


def _reader(root: Path) -> Callable[[str], str]:
    return lambda p: (root / p).read_text(encoding="utf-8", errors="ignore")


def test_checker_flags_blank_and_out_of_section_lines() -> None:
    addendum = "# A\n## 1. One\ntext one\n\n## 2. Two\ntext two\n"
    files = {
        "ok.py": "# CANON-ADDENDUM.md:3 and A§2:6",
        "blank.py": "# CANON-ADDENDUM.md:4",
        "missing.py": "# CANON-ADDENDUM.md:99",
        "wrong.py": "# A§1:6",
    }
    errors = citation_errors(files, files.__getitem__, addendum)
    assert [e.split(":", 1)[0] for e in errors] == ["blank.py", "missing.py", "wrong.py"]


def test_addendum_citations_point_at_text(repo_root: Path) -> None:
    paths = [p for p in _tracked(repo_root) if p != _PENDING_CANON_EDIT]
    addendum = (repo_root / ADDENDUM).read_text(encoding="utf-8")
    assert citation_errors(paths, _reader(repo_root), addendum) == []


@pytest.mark.xfail(strict=True, reason="CANON.md:13 addendum lines need a Canon-edit approval")
def test_canon_header_addendum_citations_point_at_text(repo_root: Path) -> None:
    addendum = (repo_root / ADDENDUM).read_text(encoding="utf-8")
    assert citation_errors([_PENDING_CANON_EDIT], _reader(repo_root), addendum) == []
