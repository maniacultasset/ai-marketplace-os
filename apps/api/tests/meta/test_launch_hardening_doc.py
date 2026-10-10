"""S26 T25 (AC 13, DW5; PLAN D10): docs/review/LAUNCH-HARDENING.md, the launch audit record.
Every check row names a test id that exists (a pytest test, a script leg or step, or a make
target), a command, a fresh-output date, a severity and a status; no P0/P1 is open; every
deferred row names its step; the named skips C2 and T41 each have a row with their trigger."""

from __future__ import annotations

import re
from pathlib import Path

DOC = "docs/review/LAUNCH-HARDENING.md"
STATUSES = frozenset({"pass", "deferred", "skip", "waived", "known gap"})
SEVERITIES = frozenset({"P0", "P1", "P2", "P3"})
CHECK_COLUMNS = ("check", "test id", "command", "fresh output", "severity", "status")


def _text(repo_root: Path) -> str:
    return (repo_root / DOC).read_text(encoding="utf-8")


def _table(text: str, header: tuple[str, ...]) -> list[dict[str, str]]:
    """The rows of the first markdown table whose header is `header`."""
    lines = text.splitlines()
    want = "| " + " | ".join(header) + " |"
    start = lines.index(want)
    rows: list[dict[str, str]] = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        assert len(cells) == len(header), line
        rows.append(dict(zip(header, cells, strict=True)))
    return rows


def _ids(cell: str) -> list[str]:
    return [part.strip().strip("`") for part in cell.split(";") if part.strip()]


def _exists(repo_root: Path, test_id: str) -> bool:
    if test_id.startswith("make "):
        target = test_id.removeprefix("make ").split()[0]
        makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
        return re.search(rf"^{re.escape(target)}:", makefile, re.MULTILINE) is not None
    if "::" in test_id:
        path, name = test_id.split("::", 1)
        name = name.split("[", 1)[0]
        file = repo_root / path
        if not file.is_file():
            return False
        return (
            re.search(rf"^(async )?def {re.escape(name)}\(", file.read_text("utf-8"), re.M)
            is not None
        )
    if "#" in test_id:
        path, leg = test_id.split("#", 1)
        file = repo_root / path
        if not file.is_file():
            return False
        body = file.read_text(encoding="utf-8")
        return re.search(rf"^(leg|step) {re.escape(leg)}$", body, re.MULTILINE) is not None or (
            f'"{leg}"' in body
        )
    return False


def test_every_check_row_has_test_id_and_status(repo_root: Path) -> None:
    rows = _table(_text(repo_root), CHECK_COLUMNS)
    assert len(rows) >= 40
    for row in rows:
        assert row["check"] and row["test id"] and row["command"], row
        assert row["status"] in STATUSES, row
        assert row["severity"] in SEVERITIES, row
        if row["status"] in ("pass", "waived", "known gap"):
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["fresh output"]), row


def test_launch_hardening_no_open_p0_p1(repo_root: Path) -> None:
    text = _text(repo_root)
    assert "**Open P0/P1: 0**" in text
    for row in _table(text, CHECK_COLUMNS):
        if row["severity"] in ("P0", "P1"):
            assert row["status"] in ("pass", "deferred"), row


def test_launch_hardening_test_ids_exist(repo_root: Path) -> None:
    for row in _table(_text(repo_root), CHECK_COLUMNS):
        for test_id in _ids(row["test id"]):
            if test_id == "-":
                assert row["status"] == "deferred", row
                continue
            assert _exists(repo_root, test_id), (row["check"], test_id)


def test_launch_hardening_deferred_rows_name_their_step(repo_root: Path) -> None:
    text = _text(repo_root)
    deferred = [r for r in _table(text, CHECK_COLUMNS) if r["status"] == "deferred"]
    assert deferred
    for row in deferred:
        assert re.search(r"\b(S\d+|T\d+|DW\d+)\b", row["command"] + " " + row["check"]), row
    for row in _table(text, ("deferred item", "to", "why")):
        assert re.search(r"\b(S\d+|T\d+|DW\d+)\b", row["to"]), row


def test_launch_hardening_named_skip_rows_c2_t41(repo_root: Path) -> None:
    rows = _table(_text(repo_root), ("named skip", "leg", "trigger"))
    by_skip = {r["named skip"]: r for r in rows}
    assert set(by_skip) == {"C2", "T41"}
    assert by_skip["C2"]["leg"] == "`scripts/e2e/acceptance.sh#ask_question_skip`"
    assert by_skip["T41"]["leg"] == "`scripts/e2e/acceptance.sh#remap_skip`"
    for row in rows:
        assert len(row["trigger"]) > 20, row
        assert _exists(repo_root, row["leg"].strip("`")), row


def test_launch_hardening_rbac_rows_name_the_switch_and_stale_role_tests(repo_root: Path) -> None:
    ids = {i for r in _table(_text(repo_root), CHECK_COLUMNS) for i in _ids(r["test id"])}
    for name in (
        "test_api2_switch_tenant_non_member_or_inactive_404",
        "test_api2_pre_switch_access_token_still_scoped_to_old_tid",
        "test_api5_switch_role_is_target_membership_role",
        "test_api5_demoted_reviewer_live_token_cannot_approve",
    ):
        assert any(i.endswith(f"::{name}") for i in ids), name


def test_launch_hardening_known_gap_open_19(repo_root: Path) -> None:
    text = _text(repo_root)
    gaps = [r for r in _table(text, CHECK_COLUMNS) if r["status"] == "known gap"]
    assert any("open #19" in r["check"] and "T131" in r["command"] for r in gaps), gaps
