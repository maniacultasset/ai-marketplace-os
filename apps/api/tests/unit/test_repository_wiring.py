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


# CODE-PLAN S12 T3: readers of platform-global tables (no `tenant_id`, DATABASE.md:158).
# S14b T5 (CD4, C6): the current category template and the dictionary revisions.
PLATFORM_READERS = frozenset(
    {"AttributeDefinitionRepository", "CategoryTemplateRepository", "DictionaryRevisionRepository"}
)


def test_every_repository_subclasses_tenant_base() -> None:
    import app.repositories
    from app.repositories.base import TenantScopedRepository

    # The one named exception (BACKEND-ARCHITECTURE.md §9; CODE-PLAN S10 Q4): the identity
    # repositories read the caller's own rows before or across a tenant, through identity_scope.
    identity = {
        "app.repositories.identity": {
            "AuditRepository",
            "MembershipRepository",
            "RefreshTokenRepository",
            "UserRepository",
        }
    }
    found = []
    for module_info in pkgutil.iter_modules(app.repositories.__path__):
        module = importlib.import_module(f"app.repositories.{module_info.name}")
        for name, obj in pyinspect.getmembers(module, pyinspect.isclass):
            if name.endswith("Repository") and obj.__module__ == module.__name__:
                found.append(name)
                if name in identity.get(module.__name__, set()) or name in PLATFORM_READERS:
                    assert not issubclass(obj, TenantScopedRepository), name
                    continue
                assert issubclass(obj, TenantScopedRepository), name
    assert "RuleRepository" in found
    assert identity["app.repositories.identity"] <= set(found)
    assert set(PLATFORM_READERS) <= set(found)


def _repository_classes() -> dict[str, Any]:
    import app.repositories

    out: dict[str, Any] = {}
    for module_info in pkgutil.iter_modules(app.repositories.__path__):
        module = importlib.import_module(f"app.repositories.{module_info.name}")
        for name, obj in pyinspect.getmembers(module, pyinspect.isclass):
            if name.endswith("Repository") and obj.__module__ == module.__name__:
                out[name] = obj
    return out


def test_platform_readers_have_no_tenant_column() -> None:
    # A platform reader is exempt from the tenant base only because its table has no tenant.
    classes = _repository_classes()
    for name in PLATFORM_READERS:
        table = classes[name].model.__table__
        assert "tenant_id" not in table.c, name


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
