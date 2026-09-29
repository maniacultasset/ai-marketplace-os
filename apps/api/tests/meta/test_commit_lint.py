"""AC15/AC16: scripts/ci/commit_lint.py applies the CANON.md:142 regexes with re.fullmatch."""

from __future__ import annotations

import os
import string
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

TYPES = ("feat", "fix", "chore", "docs", "refactor", "test", "perf")


@pytest.fixture(scope="module")
def lint(script: Callable[[str], ModuleType]) -> ModuleType:
    return script("scripts/ci/commit_lint.py")


@pytest.mark.parametrize(
    "title",
    [
        "feat(s1): backend scaffold, toolchain and CI checks",
        "fix: x",
        "chore(deps): bump fastapi",
        "docs(adr-0013): record the decision",
        "perf(api-2): faster",
        # The Canon regex accepts any non-empty summary, even a single space; tightening it
        # needs a Canon edit (CANON.md:142).
        "feat:  ",
    ],
)
def test_valid_titles_accepted(lint: ModuleType, title: str) -> None:
    assert lint.check_title(title) is True


@pytest.mark.parametrize(
    "title",
    [
        "update stuff",
        "ci: x",
        "build: x",
        "feat!: x",
        "feat(S1): x",
        "feat(s1):x",
        "feat(): x",
        "feat(s_1): x",
        "Feat: x",
        "",
        "feat: ",
        "feat: x\nsecond line",
    ],
)
def test_invalid_titles_rejected(lint: ModuleType, title: str) -> None:
    assert lint.check_title(title) is False


@pytest.mark.parametrize(
    "branch",
    ["feat/s1-scaffold", "fix/a.b_c-1", "chore/renovate-fastapi-0.x", "docs/adr-0013"],
)
def test_valid_branches_accepted(lint: ModuleType, branch: str) -> None:
    assert lint.check_branch(branch) is True


@pytest.mark.parametrize(
    "branch",
    ["main", "feature/x", "feat/S1", "feat/", "feat/a/b", "feat/ü", "feat/a b", "feat/x\n"],
)
def test_invalid_branches_rejected(lint: ModuleType, branch: str) -> None:
    assert lint.check_branch(branch) is False


def test_message_file_first_non_comment_line(lint: ModuleType, tmp_path: Path) -> None:
    text = "\n# Please enter the commit message\n\nfeat(s1): x\n\nbody line\n"
    assert lint.subject_from_message(text) == "feat(s1): x"
    assert lint.subject_from_message("# only comments\n\n") == ""
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text(text, encoding="utf-8")
    assert lint.main(["--message-file", str(message)]) == 0
    message.write_text("update stuff\n", encoding="utf-8")
    assert lint.main(["--message-file", str(message)]) == 1


# No per-example deadline: a loaded CI runner must not turn timing into a flaky failure.
@settings(deadline=None)
@given(
    kind=st.sampled_from(TYPES),
    scope=st.none() | st.from_regex(r"[a-z0-9-]+", fullmatch=True),
    summary=st.text(min_size=1).filter(lambda s: "\n" not in s and "\r" not in s),
)
def test_generated_valid_titles_accepted(
    lint: ModuleType, kind: str, scope: str | None, summary: str
) -> None:
    title = f"{kind}({scope}): {summary}" if scope is not None else f"{kind}: {summary}"
    assert lint.check_title(title) is True


@settings(deadline=None)
@given(
    kind=st.text(alphabet=string.ascii_letters + "!", min_size=1).filter(lambda k: k not in TYPES)
)
def test_unknown_type_rejected(lint: ModuleType, kind: str) -> None:
    assert lint.check_title(f"{kind}: x") is False
    assert lint.check_branch(f"{kind}/x") is False


def test_cli_exit_codes(lint: ModuleType) -> None:
    assert lint.main(["--title", "feat(s1): x", "--branch", "feat/s1-scaffold"]) == 0
    assert lint.main(["--title", "update stuff", "--branch", "feat/s1-scaffold"]) == 1
    assert lint.main(["--title", "feat(s1): x", "--branch", "feature/x"]) == 1
    assert lint.main(["--branch", "main"]) == 1
    assert lint.main([]) == 2


def test_title_is_never_evaluated(repo_root: Path, tmp_path: Path) -> None:
    payloads = [f"feat: $(touch {tmp_path}/a)", f"feat: `touch {tmp_path}/b`"]
    for payload in payloads:
        direct = subprocess.run(
            [sys.executable, "scripts/ci/commit_lint.py", "--title", payload, "--branch", "feat/x"],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
        assert direct.returncode == 0
        env = {**os.environ, "PR_TITLE": payload, "HEAD_REF": f"feat/$(touch {tmp_path}/c)"}
        via_make = subprocess.run(
            ["make", "-s", "commit-lint"], cwd=repo_root, env=env, capture_output=True, check=False
        )
        # make fails on the invalid branch; the payload title is just a string that matches.
        assert via_make.returncode != 0
        assert b"branch name does not match" in via_make.stderr
        assert b"PR title does not match" not in via_make.stderr
    assert not list(tmp_path.iterdir())
