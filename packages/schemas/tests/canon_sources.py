"""Parse Canon and related docs at test time (stdlib only)."""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_CANON = _REPO / "docs/canon/CANON.md"
_ADDENDUM = _REPO / "docs/canon/CANON-ADDENDUM.md"
_DATABASE = _REPO / "docs/architecture/DATABASE.md"
_CONTRACTS = _REPO / "docs/drafts/CONTRACTS.md"


def _read(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    return path.read_text(encoding="utf-8")


def _split_pipe_values(raw: str) -> list[str]:
    cleaned = raw.replace("\\|", "|").split("(")[0].split(".")[0]
    return [p.strip() for p in cleaned.split("|") if p.strip()]


def canon_row_values(row_name: str) -> list[str]:
    text = _read(_CANON)
    needle = f"| {row_name} |"
    for line in text.splitlines():
        if needle not in line:
            continue
        m = re.search(rf"\| {re.escape(row_name)} \| `([^`]+)`", line)
        if not m:
            raise ValueError(f"canon row {row_name!r} has no backticked span")
        inner = m.group(1)
        if "values:" in inner:
            inner = inner.split("values:", 1)[1]
        elif "PipelineStage =" in inner:
            inner = inner.split("PipelineStage =", 1)[1]
        return _split_pipe_values(inner)
    raise LookupError(f"canon row {row_name!r} not found")


def addendum_block(section: str) -> str:
    text = _read(_ADDENDUM)
    marker = f"## {section}."
    idx = text.find(marker)
    if idx < 0:
        raise LookupError(f"addendum section {section!r} not found")
    rest = text[idx:]
    fence = rest.find("```")
    if fence < 0:
        raise LookupError(f"addendum section {section!r} has no code fence")
    rest = rest[fence + 3 :]
    if rest.startswith("python"):
        rest = rest[len("python") :]
    if rest.startswith("\n"):
        rest = rest[1:]
    end = rest.find("\n```")
    if end < 0:
        raise LookupError(f"addendum section {section!r} fence unclosed")
    return rest[:end]


def database_cell_values(line_marker: str) -> list[str]:
    needle_parts = [
        p.strip()
        for p in line_marker.replace("\\|", "|").split("|")
        if p.strip()
    ]
    text = _read(_DATABASE)
    candidates: list[list[str]] = []
    for line in text.splitlines():
        if line_marker not in line and not all(p in line for p in needle_parts):
            continue
        for m in re.finditer(r"`([^`]+)`", line):
            inner = m.group(1)
            if "|" not in inner.replace("\\|", "|"):
                continue
            vals = _split_pipe_values(inner)
            if len(vals) < 2:
                continue
            candidates.append(vals)
    if not candidates:
        raise LookupError(f"database marker {line_marker!r} not found")
    if needle_parts:
        matched = [v for v in candidates if set(needle_parts).issubset(set(v))]
        if matched:
            return min(matched, key=len)
    return max(candidates, key=len)


def database_line_text(line_marker: str) -> str:
    text = _read(_DATABASE)
    for line in text.splitlines():
        if line_marker in line:
            return line
    raise LookupError(f"database marker {line_marker!r} not found")


def draft_block(heading: str) -> str:
    text = _read(_CONTRACTS)
    marker = f"### {heading}"
    idx = text.find(marker)
    if idx < 0:
        raise LookupError(f"contracts heading {heading!r} not found")
    rest = text[idx:]
    fence = rest.find("```python")
    if fence < 0:
        raise LookupError(f"contracts heading {heading!r} has no python fence")
    rest = rest[fence + len("```python") :]
    if rest.startswith("\n"):
        rest = rest[1:]
    end = rest.find("\n```")
    if end < 0:
        raise LookupError(f"contracts heading {heading!r} fence unclosed")
    return rest[:end]
