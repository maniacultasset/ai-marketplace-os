"""S9 T7: every repository subclasses the tenant base and names a TenantScoped model (EP:751)."""

from __future__ import annotations

import importlib
import inspect as pyinspect
import pkgutil
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st


def test_repository_subclass_rejects_unscoped_model() -> None:
    from app.db.models.platform import CategoryTemplate
    from app.repositories.base import TenantScopedRepository

    with pytest.raises(TypeError, match="TenantScoped"):

        class Bad(TenantScopedRepository[Any]):
            model = CategoryTemplate


def test_repository_subclass_requires_model() -> None:
    from app.repositories.base import TenantScopedRepository

    with pytest.raises(TypeError, match="model"):

        class Bad(TenantScopedRepository[Any]):
            pass


def test_every_repository_subclasses_tenant_base() -> None:
    import app.repositories
    from app.repositories.base import TenantScopedRepository

    found = []
    for module_info in pkgutil.iter_modules(app.repositories.__path__):
        module = importlib.import_module(f"app.repositories.{module_info.name}")
        for name, obj in pyinspect.getmembers(module, pyinspect.isclass):
            if name.endswith("Repository") and obj.__module__ == module.__name__:
                found.append(name)
                assert issubclass(obj, TenantScopedRepository), name
    assert "RuleRepository" in found


class _Session:
    """No SQL may reach a session for an out-of-range limit."""

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("SQL issued")


@given(st.integers().filter(lambda n: not 1 <= n <= 500))
async def test_list_limit_property(limit: int) -> None:
    from app.repositories.rules import RuleRepository

    repo = RuleRepository(_Session(), uuid4())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="limit"):
        await repo.list(limit=limit)
