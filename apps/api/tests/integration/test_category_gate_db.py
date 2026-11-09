"""SI14 T8: the categories with a current template, read from the seeded platform templates."""

from __future__ import annotations

import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import CATEGORY, GenWorld, gen_world

__all__ = ["gen_world"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


async def _allowed(world: GenWorld, lamps: bool) -> list[str]:
    from app.core.config import Settings
    from app.repositories.uow import tenant_uow
    from app.services.category_gate import allowed_categories

    settings = Settings(_env_file=None, app_env="local", category_lamps_enabled=lamps)
    async with tenant_uow(world.tenant("acme")) as uow:
        return await allowed_categories(uow, settings)


async def test_seeded_categories_and_lamps_switch(gen_world: GenWorld) -> None:
    off = await _allowed(gen_world, lamps=False)
    assert CATEGORY in off and "home_decor_lamps" not in off
    assert off == sorted(off)
    on = await _allowed(gen_world, lamps=True)
    assert "home_decor_lamps" in on
    assert set(on) - set(off) == {"home_decor_lamps"}


async def test_a_non_current_version_is_not_counted(gen_world: GenWorld) -> None:
    conn = gen_world.conn
    conn.execute("UPDATE category_templates SET is_current = false WHERE category = %s", [CATEGORY])
    conn.commit()
    assert CATEGORY not in await _allowed(gen_world, lamps=False)
