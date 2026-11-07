"""Name failed tests as GitHub annotations at the end of a pytest run.

GitHub shows a failing job's log only once the whole workflow run has finished, but annotations
are on the check run as soon as the job ends. So when `GITHUB_ACTIONS` is set, the session's
terminal summary prints one `::error` workflow command per failed or errored test (GitHub keeps
the first 10 per step), then one line with the total. Stdlib only; no effect outside Actions.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

MAX_ANNOTATIONS = 10
_MESSAGE_MAX = 300


class _Report(Protocol):
    nodeid: str
    when: str
    longreprtext: str


def _escape(text: str, *, prop: bool = False) -> str:
    """Escape a workflow command value (GitHub's toolkit rules)."""
    out = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if prop:
        out = out.replace(":", "%3A").replace(",", "%2C")
    return out


def _last_error_line(longrepr: str) -> str:
    """The most telling line of a failure: the last `E   ...` line, else the last non-blank one."""
    lines = [line.rstrip() for line in longrepr.splitlines() if line.strip()]
    errors = [line[1:].strip() for line in lines if line.startswith("E ")]
    pick = errors[-1] if errors else (lines[-1].strip() if lines else "")
    return pick[:_MESSAGE_MAX]


def annotation_lines(failed: Iterable[_Report], errored: Iterable[_Report]) -> list[str]:
    """`::error` commands for the first MAX_ANNOTATIONS problems, then a total line."""
    problems: list[tuple[str, _Report]] = [("FAILED", r) for r in failed]
    problems += [("ERROR", r) for r in errored]
    if not problems:
        return []
    lines = []
    for kind, report in problems[:MAX_ANNOTATIONS]:
        phase = "" if report.when == "call" else f" (at {report.when})"
        title = _escape(f"{kind} {report.nodeid}{phase}", prop=True)
        lines.append(f"::error title={title}::{_escape(_last_error_line(report.longreprtext))}")
    total = len(problems)
    shown = min(total, MAX_ANNOTATIONS)
    lines.append(
        f"::error title=pytest::{total} test(s) failed or errored; {shown} named above,"
        " the full list is in the job log"
    )
    return lines
