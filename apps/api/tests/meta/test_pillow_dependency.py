"""The Pillow runtime dependency: one major, locked, typed (image checks library)."""

from __future__ import annotations

import importlib.resources
import re
import tomllib
from pathlib import Path

SPEC = re.compile(r"^pillow>=(\d+)\.(\d+),<(\d+)$")


def _pillow_specs(repo_root: Path) -> list[str]:
    manifest = tomllib.loads((repo_root / "apps/api/pyproject.toml").read_text(encoding="utf-8"))
    deps: list[str] = manifest["project"]["dependencies"]
    return [d for d in deps if re.match(r"^pillow\b", d, re.IGNORECASE)]


def test_pillow_bounded_to_one_major(repo_root: Path) -> None:
    specs = _pillow_specs(repo_root)
    assert len(specs) == 1, specs
    match = SPEC.fullmatch(specs[0])
    assert match is not None, specs[0]
    major, _minor, upper = (int(g) for g in match.groups())
    assert upper == major + 1


def test_pillow_floor_clears_the_13_0_advisories(repo_root: Path) -> None:
    """12.x carries the advisories fixed in 13.0.0 (pip-audit, 2026-10-22); the floor stays above."""
    match = SPEC.fullmatch(_pillow_specs(repo_root)[0])
    assert match is not None
    assert (int(match.group(1)), int(match.group(2))) >= (13, 0), match.group(0)


def test_pillow_locked(repo_root: Path) -> None:
    match = SPEC.fullmatch(_pillow_specs(repo_root)[0])
    assert match is not None
    low = (int(match.group(1)), int(match.group(2)))
    upper = int(match.group(3))
    lock = tomllib.loads((repo_root / "uv.lock").read_text(encoding="utf-8"))
    versions = [p["version"] for p in lock["package"] if p["name"] == "pillow"]
    assert len(versions) == 1, versions
    parts = tuple(int(x) for x in versions[0].split(".")[:2])
    assert low <= parts and parts[0] < upper, versions[0]


def test_pillow_ships_types() -> None:
    assert (importlib.resources.files("PIL") / "py.typed").is_file()
