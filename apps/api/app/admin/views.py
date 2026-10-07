"""The admin views (filled in by T18 and T22)."""

from __future__ import annotations

from typing import Final

from sqladmin import Admin

OPS_PAGES: Final[tuple[str, ...]] = ()


def view_identities() -> list[str]:
    return []


def register_views(admin: Admin) -> None:
    return None
