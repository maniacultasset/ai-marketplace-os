"""The commands ONBOARDING.md tells a newcomer to run (S14b T24-T25), shared by the meta test and
`scripts/e2e/onboarding.sh`, so the document, its check and its clean-clone run never drift.

Every line of a fenced `sh` block is one command:
- `<cmd>  # background: wait <path>`: started in the background, then `GET <path>` on the API
  port is polled until it answers 200 (`make dev`, whose last recipe line is a foreground server);
- `<cmd>  # skip: <reason>`: documented, printed as `SKIP <reason>`, never run;
- anything else runs in the foreground and must exit 0.

Fenced blocks of any other language (`text` for the CI-only checks) hold no commands. A command is
a plain argv (`shlex.split`): shell metacharacters are refused, so a line can never chain, pipe
or redirect.

Usage: python3 scripts/ci/onboarding_commands.py ONBOARDING.md
prints one tab-separated `mode<TAB>wait_path<TAB>reason<TAB>command` line per command.
"""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

Mode = Literal["run", "background", "skip"]
_FENCE: Final = re.compile(r"^```\s*([A-Za-z0-9_-]*)\s*$")
_MARKER: Final = re.compile(r"\s+#\s*(background|skip):\s*(.*)$")
_WAIT: Final = re.compile(r"^wait\s+(/[A-Za-z0-9/_-]*)$")
_FORBIDDEN: Final = frozenset(";|&<>$`\\(){}*?!~\n")


class OnboardingError(ValueError):
    """A malformed command line in ONBOARDING.md."""


@dataclass(frozen=True)
class Command:
    line: str
    mode: Mode
    wait_path: str | None = None
    reason: str | None = None

    @property
    def argv(self) -> list[str]:
        return shlex.split(self.line)


def _command(raw: str, number: int) -> Command:
    marker = _MARKER.search(raw)
    line = raw[: marker.start()] if marker else raw
    line = line.strip()
    if not line or line.startswith("#"):
        raise OnboardingError(f"line {number}: an empty or comment-only command")
    bad = sorted({c for c in line if c in _FORBIDDEN})
    if bad:
        raise OnboardingError(f"line {number}: shell metacharacters {bad} are not allowed")
    if marker is None:
        return Command(line, "run")
    kind, rest = marker.group(1), marker.group(2).strip()
    if kind == "skip":
        if not rest:
            raise OnboardingError(f"line {number}: a skip needs a reason")
        return Command(line, "skip", reason=rest)
    wait = _WAIT.match(rest)
    if wait is None:
        raise OnboardingError(f"line {number}: background needs `wait /<path>`")
    return Command(line, "background", wait_path=wait.group(1))


def parse(markdown: str) -> list[Command]:
    commands: list[Command] = []
    fence: str | None = None
    for number, raw in enumerate(markdown.splitlines(), start=1):
        opened = _FENCE.match(raw.strip())
        if opened:
            fence = (opened.group(1) or "plain") if fence is None else None
            continue
        if fence != "sh" or not raw.strip():
            continue
        commands.append(_command(raw, number))
    if fence is not None:
        raise OnboardingError("an unclosed code fence")
    return commands


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: onboarding_commands.py ONBOARDING.md\n")
        return 2
    try:
        commands = parse(Path(argv[1]).read_text(encoding="utf-8"))
    except (OSError, OnboardingError) as exc:
        sys.stderr.write(f"onboarding_commands: {exc}\n")
        return 1
    for c in commands:
        print("\t".join((c.mode, c.wait_path or "", c.reason or "", c.line)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
