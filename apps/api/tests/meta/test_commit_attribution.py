"""Commit-message attribution check: trailer and footer forms only (P13)."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

SCISSORS = "# ------------------------ >8 ------------------------"


@pytest.fixture(scope="module")
def attr(script: Callable[[str], ModuleType]) -> ModuleType:
    return script("scripts/ci/commit_attribution.py")


def _write(tmp_path: Path, text: str, *, name: str = "COMMIT_EDITMSG") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_clean_message_passes(
    attr: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, "feat(s1): add hooks\n\nBody without a trailer.\n")
    assert attr.main([str(path)]) == 0
    assert "OK" in capsys.readouterr().out


@pytest.mark.parametrize(
    "line",
    [
        "Co-authored-by: A <a@x>",
        "CO-AUTHORED-BY: A",
        "co-authored-by:A",
    ],
)
def test_co_authored_by_fails(attr: ModuleType, tmp_path: Path, line: str) -> None:
    path = _write(tmp_path, f"feat: x\n\n{line}\n")
    assert attr.main([str(path)]) == 1


def test_co_authored_by_trailer_after_blank_fails(attr: ModuleType, tmp_path: Path) -> None:
    path = _write(tmp_path, "feat: x\n\n")
    path.write_text(
        path.read_text(encoding="utf-8") + "Co-authored-by: A <a@x>\n", encoding="utf-8"
    )
    assert attr.main([str(path)]) == 1


@pytest.mark.parametrize(
    "line",
    [
        "Generated with X",
        "generated  with x",
        "🤖 Generated with X",
    ],
)
def test_generated_with_fails(attr: ModuleType, tmp_path: Path, line: str) -> None:
    path = _write(tmp_path, f"feat: x\n\n{line}\n")
    assert attr.main([str(path)]) == 1


@pytest.mark.parametrize(
    "line",
    [
        "the file was generated with compile_trace.py",
        "regenerated with compile_trace.py",
    ],
)
def test_generated_with_mid_sentence_and_regenerated_pass(
    attr: ModuleType, tmp_path: Path, line: str
) -> None:
    path = _write(tmp_path, f"feat: x\n\n{line}\n")
    assert attr.main([str(path)]) == 0


def test_hash_prefixed_generated_with_fails(attr: ModuleType, tmp_path: Path) -> None:
    path = _write(tmp_path, "feat: x\n\n# Generated with X\n")
    assert attr.main([str(path)]) == 1
    path = _write(tmp_path, "feat: x\n\n# Co-authored-by: A\n")
    assert attr.main([str(path)]) == 0


def test_scissors_section(attr: ModuleType, tmp_path: Path) -> None:
    hidden = f"feat: x\n\n{SCISSORS}\ndiff --git a/f b/f\nCo-authored-by: A <a@x>\n"
    assert attr.main([str(_write(tmp_path, hidden, name="hidden"))]) == 0
    visible = f"feat: x\n\n{SCISSORS}\nCo-authored-by: A <a@x>\n"
    assert attr.main([str(_write(tmp_path, visible, name="visible"))]) == 1
    above = f"Co-authored-by: A <a@x>\n{SCISSORS}\ndiff --git a/f b/f\n"
    assert attr.main([str(_write(tmp_path, above, name="above"))]) == 1


def test_reports_line_numbers_never_the_body(
    attr: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, "feat: subject-only\n\nCo-authored-by: A <a@x>\n")
    assert attr.main([str(path)]) == 1
    err = capsys.readouterr().err
    assert "line 3" in err
    assert "Co-authored-by" not in err
    assert "subject-only" not in err


@pytest.mark.parametrize("text", ["", "\n\n"])
def test_empty_and_blank_messages_pass(attr: ModuleType, tmp_path: Path, text: str) -> None:
    path = _write(tmp_path, text)
    assert attr.main([str(path)]) == 0


def test_crlf_and_invalid_utf8(attr: ModuleType, tmp_path: Path) -> None:
    crlf = tmp_path / "crlf"
    crlf.write_bytes(b"feat: x\r\n\r\nCo-authored-by: A <a@x>\r\n")
    assert attr.main([str(crlf)]) == 1
    bad = tmp_path / "bad"
    bad.write_bytes(b"\xff\xfe\nCo-authored-by: A <a@x>\n")
    assert attr.main([str(bad)]) == 1


def test_missing_file_exits_2(attr: ModuleType, tmp_path: Path) -> None:
    assert attr.main([str(tmp_path / "missing")]) == 2


def test_runs_as_isolated_subprocess(repo_root: Path, tmp_path: Path) -> None:
    path = _write(tmp_path, "feat: x\n\nCo-authored-by: A <a@x>\n")
    script = repo_root / "scripts" / "ci" / "commit_attribution.py"
    result = subprocess.run(
        [sys.executable, "-I", str(script), str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
