"""The failed-test annotations printed on GitHub Actions (tests/support/gh_annotations.py)."""

from __future__ import annotations

from dataclasses import dataclass

from tests.support.gh_annotations import MAX_ANNOTATIONS, annotation_lines


@dataclass
class _R:
    nodeid: str
    when: str = "call"
    longreprtext: str = ""


def test_nothing_failed_prints_nothing() -> None:
    assert annotation_lines([], []) == []


def test_failed_test_named_with_its_last_error_line() -> None:
    report = _R(
        "tests/x/test_a.py::test_one[owner]",
        longreprtext="def test_one():\n>   assert 1 == 2\nE   assert 1 == 2\nE    +  where 1 = f()\n",
    )
    lines = annotation_lines([report], [])
    assert lines[0] == (
        "::error title=FAILED tests/x/test_a.py%3A%3Atest_one[owner]::+  where 1 = f()"
    )
    assert lines[-1].startswith("::error title=pytest::1 test(s) failed or errored; 1 named above")


def test_setup_error_names_the_phase_and_escapes_newlines_and_percent() -> None:
    report = _R("t.py::test_b", when="setup", longreprtext="KeyError: 'task_id'\n100% broken\n")
    line = annotation_lines([], [report])[0]
    assert line.startswith("::error title=ERROR t.py%3A%3Atest_b (at setup)::")
    assert line.endswith("100%25 broken")
    assert "\n" not in line


def test_capped_at_ten_named_then_a_total() -> None:
    reports = [_R(f"t.py::test_{i}", longreprtext="E   boom") for i in range(15)]
    lines = annotation_lines(reports, [])
    assert len(lines) == MAX_ANNOTATIONS + 1
    assert "15 test(s) failed or errored; 10 named above" in lines[-1]
