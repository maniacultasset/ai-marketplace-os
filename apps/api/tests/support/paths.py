"""The repository root for the test suites (S26 T23): the nearest parent holding both the root
`Makefile` and `apps/api`, so the suites also run from a copy of `apps/api` one level deeper
(mutmut's `apps/api/mutants/`), where a fixed `parents[n]` would land on the wrong folder."""

from __future__ import annotations

from pathlib import Path


def repo_root(start: Path | None = None) -> Path:
    here = (start or Path(__file__)).resolve()
    for parent in here.parents:
        if (parent / "Makefile").is_file() and (parent / "apps" / "api").is_dir():
            return parent
    raise RuntimeError(f"no repository root above {here}")
