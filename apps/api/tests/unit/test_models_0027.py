"""SI14 T2: the 0027 ORM columns (the DDL is the revision's; `alembic check` proves parity)."""

from __future__ import annotations

from app.db.models._tenant import TenantScoped
from app.db.models.catalog import Product
from app.db.models.issues import HumanTask
from app.db.models.ops import Import, ImportColumnMap
from app.services.category_gate import UNCATEGORIZED, rules_category
from sqlalchemy import Table


def test_product_category_is_nullable() -> None:
    table = Product.__table__
    assert table.c.category.nullable
    assert table.c.category_suggestion.nullable


def test_import_column_map_is_tenant_scoped() -> None:
    assert issubclass(ImportColumnMap, TenantScoped)
    table = ImportColumnMap.__table__
    assert table.c.samples.nullable and table.c.job_id.nullable
    assert not table.c.headers.nullable and not table.c.mappings.nullable
    assert Import.__table__.c.column_map_id.nullable


def test_one_live_category_review_index() -> None:
    table = HumanTask.__table__
    assert isinstance(table, Table)
    index = next(i for i in table.indexes if i.name == "human_tasks_one_category_review")
    assert index.unique
    assert [c.name for c in index.columns] == ["tenant_id", "product_id"]


def test_rules_category_stand_in() -> None:
    assert rules_category("t_shirts") == "t_shirts"
    assert rules_category(None) == UNCATEGORIZED
