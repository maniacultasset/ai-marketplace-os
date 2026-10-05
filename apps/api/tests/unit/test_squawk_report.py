"""scripts/ci/squawk_report.py: squawk over the rendered migration SQL, report-only until the
first prod tag (EXECUTION-PLAN.md:303-305; DATABASE.md §8.3 squawk rules). A finding prints and passes; a
tool failure (missing binary, crash, non-JSON output, unparsable SQL) fails closed."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = "scripts/ci/squawk_report.py"
SQUAWK = str(Path(sys.executable).parent / "squawk")
TAG = "[DATABASE.md §8.3]"


@pytest.fixture
def squawk_report(script: Callable[[str], ModuleType]) -> ModuleType:
    return script(SCRIPT)


def _sql(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "rendered.sql"
    path.write_text(text, encoding="utf-8")
    return path


def _fake_squawk(tmp_path: Path, body: str) -> str:
    path = tmp_path / "fake-squawk"
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_squawk_report_only_prints_findings_exit_zero(
    squawk_report: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sql = _sql(tmp_path, "CREATE INDEX ix_a ON t (b);\n")
    assert squawk_report.report(sql, squawk=SQUAWK) == 0
    out = capsys.readouterr().out
    assert re.search(rf"require-concurrent-index-creation {re.escape(TAG)} ", out)
    assert re.search(r"squawk: \d+ findings \(\d+ on DATABASE\.md §8\.3 rules\); report-only", out)


def test_squawk_report_tags_only_listed_rules(
    squawk_report: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sql = _sql(tmp_path, "ALTER TABLE t ADD COLUMN c int;\n")
    assert squawk_report.report(sql, squawk=SQUAWK) == 0
    lines = capsys.readouterr().out.splitlines()
    bigint = [line for line in lines if " prefer-bigint-over-int " in line]
    assert bigint, lines
    assert all(TAG not in line for line in bigint)
    assert any(" prefer-robust-stmts " + TAG in line for line in lines)


def test_squawk_report_empty_sql_ok(
    squawk_report: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert squawk_report.report(_sql(tmp_path, ""), squawk=SQUAWK) == 0
    assert capsys.readouterr().out == "squawk: 0 findings\n"


def test_squawk_tool_failure_missing_binary(
    squawk_report: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert squawk_report.report(_sql(tmp_path, ""), squawk="/nonexistent/squawk") == 1
    assert "squawk_report: squawk did not check the SQL" in capsys.readouterr().err


@pytest.mark.parametrize(
    "body", ["exit 2", "echo 'not json'; exit 1", "echo '{}'; exit 1", "echo '[]'; exit 1"]
)
def test_squawk_tool_failure_crash(
    squawk_report: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], body: str
) -> None:
    fake = _fake_squawk(tmp_path, body)
    assert squawk_report.report(_sql(tmp_path, "SELECT 1;\n"), squawk=fake) == 1
    assert "squawk_report: squawk did not check the SQL" in capsys.readouterr().err


def test_squawk_syntax_error_fails(
    squawk_report: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert squawk_report.report(_sql(tmp_path, "CREATE TABLE (;\n"), squawk=SQUAWK) == 1
    assert "syntax-error" in capsys.readouterr().err


def test_squawk_rules_match_database_md(squawk_report: ModuleType, repo_root: Path) -> None:
    text = (repo_root / "docs/architecture/DATABASE.md").read_text(encoding="utf-8")
    (line,) = [ln for ln in text.splitlines() if ln.startswith("- **squawk rules:**")]
    head = line.split(".", 1)[0]  # the rule list ends at the first sentence
    names = tuple(re.findall(r"`([a-z-]+)`", head))
    assert names == squawk_report.DATABASE_MD_RULES
    assert len(names) == 9


def test_squawk_runs_lint_only(squawk_report: ModuleType, repo_root: Path) -> None:
    argv = squawk_report.squawk_argv("squawk", Path("x.sql"))
    assert argv == ["squawk", "--reporter", "json", "--pg-version", "17", "x.sql"]
    text = (repo_root / SCRIPT).read_text(encoding="utf-8")
    for banned in ("upload-to-github", "server", "--assume-in-transaction"):
        assert banned not in argv
        assert banned not in text


def test_squawk_main_takes_one_path(
    squawk_report: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(squawk_report, "DEFAULT_SQUAWK", SQUAWK)
    assert squawk_report.main([str(_sql(tmp_path, ""))]) == 0
    with pytest.raises(SystemExit) as info:
        squawk_report.main([])
    assert info.value.code == 2


def test_squawk_tags_a_timestamp_column_with_the_reported_rule_name(
    squawk_report: ModuleType, tmp_path: Path
) -> None:
    # squawk accepts `prefer-timestamptz` as an alias on the command line but reports the finding as
    # `prefer-timestamp-tz`; the listed name must be the reported one or the finding is never tagged.
    findings = squawk_report._run(_sql(tmp_path, "CREATE TABLE t (a timestamp);\n"), SQUAWK)
    names = {str(f["rule_name"]) for f in findings}
    timestamp_rules = {n for n in names if "timestamp" in n}
    assert timestamp_rules, names
    assert timestamp_rules <= set(squawk_report.DATABASE_MD_RULES), names
