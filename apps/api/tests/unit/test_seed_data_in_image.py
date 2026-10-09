"""S20: the image's local-only `seed` role loads the fake error map, so the file lives in the
app tree the image copies, never under `apps/api/tests/` (excluded by `.dockerignore`)."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[4]


def test_error_map_fake_ships_in_the_image() -> None:
    from app.cli import ERROR_MAP_FAKE_PATH

    assert ERROR_MAP_FAKE_PATH.is_file()
    relative = ERROR_MAP_FAKE_PATH.resolve().relative_to(REPO).as_posix()
    assert relative.startswith("apps/api/app/"), relative
    ignored = [
        line.strip().rstrip("/")
        for line in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith(("#", "!"))
    ]
    assert not [rule for rule in ignored if relative.startswith(f"{rule}/")], ignored
