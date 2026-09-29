"""The `commit-lint` check and the `commit-msg` hook (CANON.md:142; AGENTS.md §6).

The PR title becomes the squash commit, so it must be a conventional-commit subject, and the
branch name must match the branch rule. Both come in through argv (from `env:` in CI), and are
only ever matched, never evaluated. Stdlib only: CI runs it on the runner's python3.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TITLE_RE = r"^(feat|fix|chore|docs|refactor|test|perf)(\([a-z0-9-]+\))?: .+"
BRANCH_RE = r"^(feat|fix|chore|docs|refactor|test|perf)/[a-z0-9._-]+$"


def check_title(title: str) -> bool:
    return re.fullmatch(TITLE_RE, title) is not None


def check_branch(branch: str) -> bool:
    return re.fullmatch(BRANCH_RE, branch) is not None


def subject_from_message(text: str) -> str:
    """The first line that is neither blank nor a `#` comment (git strips those)."""
    for line in text.splitlines():
        if line.strip() and not line.startswith("#"):
            return line
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a PR title, branch or commit message.")
    parser.add_argument("--title", help="the PR title (the squash commit subject)")
    parser.add_argument("--branch", help="the head branch name")
    parser.add_argument("--message-file", type=Path, help="a commit message file (commit-msg)")
    args = parser.parse_args(argv)
    if args.title is None and args.branch is None and args.message_file is None:
        print("commit-lint: give --title, --branch or --message-file", file=sys.stderr)
        return 2
    failures = []
    if args.title is not None and not check_title(args.title):
        failures.append(f"PR title does not match {TITLE_RE}")
    if args.branch is not None and not check_branch(args.branch):
        failures.append(f"branch name does not match {BRANCH_RE}")
    if args.message_file is not None:
        subject = subject_from_message(args.message_file.read_text(encoding="utf-8"))
        if not check_title(subject):
            failures.append(f"commit subject does not match {TITLE_RE}")
    for failure in failures:
        print(f"commit-lint: {failure}", file=sys.stderr)
    if not failures:
        print("commit-lint: OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
