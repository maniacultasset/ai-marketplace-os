"""S9 T3 (AC 12): every model holding a `tenant_id` column is `TenantScoped`; `tenants` is
scoped on its own `id`; only `rules` may read platform (NULL-tenant) rows."""

from __future__ import annotations

import app.db.models  # noqa: F401  (registers every mapper)
from app.db.base import Base
from app.db.models._tenant import TenantScoped
from app.db.models.rules import Rule
from app.db.models.tenancy import Tenant
from sqlalchemy.orm import RelationshipProperty


def _mapped_classes() -> list[type]:
    return [mapper.class_ for mapper in Base.registry.mappers]


def test_every_tenant_id_table_is_tenant_scoped() -> None:
    missing = [
        cls.__name__
        for cls in _mapped_classes()
        if "tenant_id" in cls.__table__.c and not issubclass(cls, TenantScoped)  # type: ignore[attr-defined]
    ]
    assert missing == []


def test_no_platform_table_is_tenant_scoped() -> None:
    wrong = [
        cls.__name__
        for cls in _mapped_classes()
        if issubclass(cls, TenantScoped) and cls.__tenant_column__ not in cls.__table__.c  # type: ignore[attr-defined]
    ]
    assert wrong == []


def test_tenants_scoped_on_id() -> None:
    assert issubclass(Tenant, TenantScoped)
    assert Tenant.__tenant_column__ == "id"
    assert Tenant.tenant_col() is Tenant.id


def test_only_rule_reads_platform_rows() -> None:
    platform = [
        cls.__name__
        for cls in _mapped_classes()
        if issubclass(cls, TenantScoped) and cls.__platform_rows__
    ]
    assert platform == [Rule.__name__]


def test_relationships_default_raise() -> None:
    lazy = [
        f"{mapper.class_.__name__}.{rel.key}={rel.lazy}"
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, TenantScoped)
        for rel in mapper.relationships
        if isinstance(rel, RelationshipProperty) and rel.lazy not in ("raise", "raise_on_sql")
    ]
    assert lazy == []
