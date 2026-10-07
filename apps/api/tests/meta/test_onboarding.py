"""S14b T24 (AC 15; Done-when 7): ONBOARDING.md holds the invariants and the ownership line, and
every command it tells a newcomer to run exists and is marked for the clean-clone run."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

PARSER = "scripts/ci/onboarding_commands.py"
CI_ONLY = ("commit-lint", "gitleaks", "bootstrap")
TOOL_NAMES = re.compile(r"\b(claude|codex|chatgpt|copilot|cursor|anthropic|openai codex)\b", re.I)


def _onboarding(repo_root: Path) -> str:
    return (repo_root / "ONBOARDING.md").read_text(encoding="utf-8")


def _make_targets(repo_root: Path) -> dict[str, list[str]]:
    """target -> its recipe lines, from the root Makefile."""
    targets: dict[str, list[str]] = {}
    current: str | None = None
    for line in (repo_root / "Makefile").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([a-z][a-z0-9-]*):(?!=)", line)
        if match:
            current = match.group(1)
            targets[current] = []
        elif current and line.startswith("\t"):
            targets[current].append(line.strip())
        elif line and not line.startswith(("\t", "#", " ")):
            current = None
    return targets


def _cli_paths() -> set[tuple[str, ...]]:
    """Every subcommand path of `python -m app.cli` (its argparse tree)."""
    from app import cli

    captured: list[argparse.ArgumentParser] = []
    real = argparse.ArgumentParser.parse_args

    def grab(self: argparse.ArgumentParser, *_: Any, **__: Any) -> Any:
        captured.append(self)
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = grab  # type: ignore[method-assign]
    try:
        cli.main(["--help"])
    except SystemExit:
        pass
    finally:
        argparse.ArgumentParser.parse_args = real  # type: ignore[method-assign]
    paths: set[tuple[str, ...]] = set()

    def walk(parser: argparse.ArgumentParser, prefix: tuple[str, ...]) -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    paths.add((*prefix, name))
                    walk(sub, (*prefix, name))

    for parser in captured:
        walk(parser, ())
    return paths


def test_onboarding_ownership_line_verbatim(repo_root: Path) -> None:
    canon = (repo_root / "docs/canon/CANON.md").read_text(encoding="utf-8")
    (line,) = [ln for ln in canon.splitlines() if ln.startswith("- Ownership line:")]
    assert line in _onboarding(repo_root).splitlines()


def test_onboarding_ten_invariants(repo_root: Path) -> None:
    text = _onboarding(repo_root)
    section = text.split("## The 10 invariants", 1)[1].split("\n## ", 1)[0]
    numbered = re.findall(r"(?m)^(\d+)\. \S", section)
    assert numbered == [str(n) for n in range(1, 11)]
    assert "(docs/RULES.md)" in section and "§1" in section


def test_onboarding_commands(repo_root: Path, script: Callable[[str], Any]) -> None:
    parser = script(PARSER)
    commands = parser.parse(_onboarding(repo_root))
    assert commands
    targets = _make_targets(repo_root)
    tracked = set(
        subprocess.check_output(["git", "-C", str(repo_root), "ls-files"], text=True).splitlines()
    )
    cli_paths = _cli_paths()
    for command in commands:
        argv = command.argv
        if argv[0] == "make":
            assert len(argv) >= 2 and argv[1] in targets, command.line
            recipe = targets[argv[1]]
            serves = bool(recipe) and ("uvicorn" in recipe[-1] or "--reload" in recipe[-1])
            if serves:
                assert command.mode == "background", command.line
            if argv[1] in CI_ONLY:
                assert command.mode == "skip", command.line
        elif argv[:3] in (["uv", "run", "python"], ["python3", "-m", "app.cli"]):
            assert tuple(a for a in argv if not a.startswith("-"))[3:] in cli_paths
        else:
            assert argv[0] in tracked and os.access(repo_root / argv[0], os.X_OK), command.line
        if command.mode == "skip":
            assert command.reason, command.line
    dev = [c for c in commands if c.argv == ["make", "dev"]]
    assert len(dev) == 1 and dev[0].mode == "background" and dev[0].wait_path == "/healthz"
    for name in CI_ONLY:
        assert not [c for c in commands if c.argv[:2] == ["make", name] and c.mode != "skip"]


@pytest.mark.parametrize("kind", ["run", "background", "skip", "text_block", "refusals"])
def test_onboarding_parser_markers(script: Callable[[str], Any], kind: str) -> None:
    parser = script(PARSER)
    if kind == "run":
        (c,) = parser.parse("```sh\nmake seed\n```\n")
        assert (c.mode, c.argv) == ("run", ["make", "seed"])
    elif kind == "background":
        (c,) = parser.parse("```sh\nmake dev  # background: wait /healthz\n```\n")
        assert (c.mode, c.wait_path, c.line) == ("background", "/healthz", "make dev")
    elif kind == "skip":
        (c,) = parser.parse("```sh\nmake x  # skip: needs CI\n```\n")
        assert (c.mode, c.reason) == ("skip", "needs CI")
    elif kind == "text_block":
        assert parser.parse("```text\nmake gitleaks\n```\n```\nmake y\n```\n") == []
    else:
        for bad in (
            "```sh\nmake x; rm -rf /\n```\n",
            "```sh\nmake x | tee y\n```\n",
            "```sh\nmake $TARGET\n```\n",
            "```sh\nmake x  # skip:\n```\n",
            "```sh\nmake dev  # background: soon\n```\n",
            "```sh\nmake x\n",
        ):
            with pytest.raises(parser.OnboardingError):
                parser.parse(bad)


def test_onboarding_links_tracked(repo_root: Path) -> None:
    tracked = set(
        subprocess.check_output(["git", "-C", str(repo_root), "ls-files"], text=True).splitlines()
    )
    links = re.findall(r"\]\(([^)#\s]+)", _onboarding(repo_root))
    assert links
    for link in links:
        assert "://" not in link, link
        assert link in tracked, link


def test_onboarding_no_tool_names_or_local_paths(repo_root: Path) -> None:
    text = _onboarding(repo_root)
    assert "docs/plans/" not in text
    assert "deliverables/" not in text
    assert not TOOL_NAMES.search(text), TOOL_NAMES.search(text)
