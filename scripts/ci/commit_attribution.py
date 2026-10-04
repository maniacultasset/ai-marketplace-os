"""Reject attribution trailers and 'Generated with' footers in a commit message (RULES.md:161).

Stdlib only. Reads the message file passed by the commit-msg hook. A custom
`core.commentChar` is not supported (`#` only, same as commit_lint.py).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

CO_AUTHORED = re.compile(r"^\s*co-authored-by\s*:", re.IGNORECASE)
GENERATED = re.compile(r"^\W*generated\s+with\b", re.IGNORECASE)
SCISSORS = "# ------------------------ >8 ------------------------"


def offending_lines(text: str) -> list[int]:
    """1-based line numbers that match a forbidden trailer or footer."""
    lines = text.splitlines()
    hits: list[int] = []
    for index, line in enumerate(lines):
        if (
            line == SCISSORS
            and index + 1 < len(lines)
            and lines[index + 1].startswith("diff --git")
        ):
            break
        if CO_AUTHORED.match(line) or GENERATED.match(line):
            hits.append(index + 1)
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reject attribution trailers and footers.")
    parser.add_argument("message_file", type=Path)
    args = parser.parse_args(argv)
    try:
        text = args.message_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"commit-attribution: {exc.strerror}", file=sys.stderr)
        return 2
    hits = offending_lines(text)
    if hits:
        for number in hits:
            print(
                f"commit-attribution: line {number}: attribution "
                "trailer or footer is not allowed (RULES.md:161)",
                file=sys.stderr,
            )
        return 1
    print("commit-attribution: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
