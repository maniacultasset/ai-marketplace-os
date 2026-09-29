"""AC15: the pip-audit ignore list is dated, and an expired or malformed entry fails the gate."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from types import ModuleType

import pytest

TODAY = date(2026, 9, 29)


@pytest.fixture
def audit(script: Callable[[str], ModuleType]) -> ModuleType:
    return script("scripts/ci/run_pip_audit.py")


def test_empty_file_ok(audit: ModuleType) -> None:
    assert audit.parse_ignore_file("", TODAY) == []


def test_live_entry_ignored(audit: ModuleType) -> None:
    text = "GHSA-aaaa-bbbb-cccc 2026-10-15 fix pending upstream\nPYSEC-2026-1 2026-09-29 today\n"
    assert audit.parse_ignore_file(text, TODAY) == ["GHSA-aaaa-bbbb-cccc", "PYSEC-2026-1"]


def test_expired_entry_fails(audit: ModuleType) -> None:
    with pytest.raises(ValueError, match="expired"):
        audit.parse_ignore_file("GHSA-aaaa-bbbb-cccc 2026-09-28 was due yesterday\n", TODAY)


@pytest.mark.parametrize(
    "line",
    [
        "GHSA-aaaa-bbbb-cccc no date here",
        "GHSA-aaaa-bbbb-cccc 2026-13-01 bad month",
        "GHSA-aaaa-bbbb-cccc 2026-10-15",
        "GHSA-aaaa-bbbb-cccc",
    ],
)
def test_malformed_entry_fails(audit: ModuleType, line: str) -> None:
    with pytest.raises(ValueError, match="malformed"):
        audit.parse_ignore_file(line + "\n", TODAY)


def test_comments_and_blanks_skipped(audit: ModuleType) -> None:
    text = "# <ID> <YYYY-MM-DD> <reason>\n\n   \n# GHSA-x 2020-01-01 old\n"
    assert audit.parse_ignore_file(text, TODAY) == []
