"""The `TenantScoped` mixin (S9 T3; invariant 5). Every model that holds a `tenant_id` column
inherits it, and `tenants` itself is scoped on its `id`. The repository base
(`app/repositories/base.py`) and the guard (`app/db/guard.py`) read these markers; no model
declares a relationship that lazy-loads, so a scoped statement is the only way rows load."""

from __future__ import annotations

from typing import Any, ClassVar

from sqlalchemy.orm import InstrumentedAttribute


class TenantScoped:
    __tenant_column__: ClassVar[str] = "tenant_id"
    # True only on `Rule`: platform rows (`tenant_id IS NULL`) are readable next to a tenant's own.
    __platform_rows__: ClassVar[bool] = False

    @classmethod
    def tenant_col(cls) -> InstrumentedAttribute[Any]:
        column: InstrumentedAttribute[Any] = getattr(cls, cls.__tenant_column__)
        return column
