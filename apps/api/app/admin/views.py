"""The 12 read-only admin views (S14b T18; BUILD-SEQUENCE.md:235 (3); DATABASE.md:1015, :1750).

- One `ModelView` per table, every write flag False (create, edit, delete, export).
- `ADMIN_COLUMNS` is the literal column map: the list and detail pages show only these columns,
  and the list, detail and count queries load only these (`load_only`), so no statement ever
  names a column the read-only role cannot SELECT. `token_enc`, `hashed_password`,
  `token_hash`, `state_hash`, `code_verifier`, the audit actor e-mail and IP and the image
  `content_hash` are never listed.
- Every query runs in `platform_scope()` (the `PlatformSessionMaker` passed to `Admin`).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, ClassVar, Final

from sqladmin import Admin, ModelView
from sqlalchemy import Select, func, select
from sqlalchemy.orm import load_only
from starlette.exceptions import HTTPException
from starlette.requests import Request

from app.db.models.audit_brand import AuditLog
from app.db.models.catalog import Product
from app.db.models.catalog_assets import Image, MarketplaceAccount
from app.db.models.listings import Listing, ListingVersion
from app.db.models.ops import JobRun
from app.db.models.rules import Rule
from app.db.models.runs import AgentRun, ValidationRun
from app.db.models.tenancy import Tenant, User

ADMIN_COLUMNS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "tenants": ("id", "name", "slug", "status", "plan", "auto_publish", "created_at"),
        "users": ("id", "email", "display_name", "is_active", "last_login_at", "created_at"),
        "products": ("id", "tenant_id", "sku", "title", "status", "category", "created_at"),
        "listings": (
            "id",
            "tenant_id",
            "product_id",
            "marketplace",
            "category",
            "status",
            "status_changed_at",
            "current_version_id",
            "created_at",
        ),
        "listing_versions": (
            "id",
            "tenant_id",
            "listing_id",
            "version",
            "origin",
            "content_json",
            "rules_version",
            "created_at",
        ),
        "validation_runs": (
            "id",
            "tenant_id",
            "listing_id",
            "listing_version_id",
            "source",
            "passed",
            "rules_version",
            "error_report_json",
            "created_at",
        ),
        "agent_runs": (
            "id",
            "tenant_id",
            "job_id",
            "agent_name",
            "model_used",
            "status",
            "cost_usd",
            "tokens_in",
            "tokens_out",
            "created_at",
        ),
        "job_runs": (
            "id",
            "tenant_id",
            "kind",
            "queue",
            "status",
            "node",
            "attempt",
            "error_code",
            "queued_at",
            "finished_at",
        ),
        "images": (
            "id",
            "tenant_id",
            "product_id",
            "slot",
            "status",
            "compliance_status",
            "image_report_json",
            "created_at",
        ),
        "rules": (
            "id",
            "tenant_id",
            "marketplace",
            "category",
            "field",
            "rule_type",
            "severity",
            "version",
            "enabled",
            "revision",
            "created_at",
        ),
        "marketplace_accounts": (
            "id",
            "tenant_id",
            "marketplace",
            "seller_id",
            "status",
            "expires_at",
            "created_at",
        ),
        "audit_log": (
            "id",
            "tenant_id",
            "actor_type",
            "action",
            "entity_type",
            "entity_id",
            "occurred_at",
        ),
    }
)
NEVER_LISTED: Final = frozenset(
    {
        "token_enc",
        "hashed_password",
        "token_hash",
        "state_hash",
        "code_verifier",
        "actor_email",
        "ip",
        "content_hash",
        "raw_row",
    }
)
OPS_PAGES: Final[tuple[str, ...]] = ()


class ReadOnlyView(ModelView):
    """No create, edit, delete or export; queries load only the `ADMIN_COLUMNS` of the table."""

    can_create = False
    can_edit = False
    can_delete = False
    can_export = False
    can_view_details = True
    page_size = 50
    page_size_options: ClassVar[list[int]] = [25, 50, 100]
    table: ClassVar[str]

    def _columns(self) -> list[Any]:
        return [getattr(self.model, name) for name in ADMIN_COLUMNS[self.table]]

    def list_query(self, request: Request) -> Select[Any]:
        return select(self.model).options(load_only(*self._columns()))

    def details_query(self, request: Request) -> Select[Any]:
        try:
            uuid.UUID(str(request.path_params.get("pk", "")))
        except ValueError:
            raise HTTPException(status_code=404) from None  # every admin table has a UUID id
        stmt: Select[Any] = super().details_query(request)
        return stmt.options(load_only(*self._columns()))

    def count_query(self, request: Request) -> Select[Any]:
        model: Any = self.model
        return select(func.count(model.id))

    async def count(self, request: Request, stmt: Select[Any] | None = None) -> int:
        """Always `count_query`: sqladmin's list page counts over a subquery of the list query,
        and a subquery drops `load_only`, so it would name every column (a secret one included).
        The views have no filter and no search, so the plain count is the list's count."""
        return await super().count(request, self.count_query(request))

    async def check_can_create(self, request: Request) -> bool:
        return False

    async def check_can_edit(self, request: Request, model: Any) -> bool:
        return False

    async def check_can_delete(self, request: Request, model: Any) -> bool:
        return False

    async def check_can_export(self, request: Request) -> bool:
        return False


def _view(model: type[Any], table: str, label: str) -> type[ReadOnlyView]:
    columns = [getattr(model, name) for name in ADMIN_COLUMNS[table]]
    attrs: dict[str, Any] = {
        "table": table,
        "name": label,
        "name_plural": label,
        "column_list": columns,
        "column_details_list": columns,
        "column_sortable_list": [],
        "column_searchable_list": [],
    }
    return type(f"{model.__name__}Admin", (ReadOnlyView,), attrs, model=model)


ADMIN_VIEWS: Final[tuple[type[ReadOnlyView], ...]] = (
    _view(Tenant, "tenants", "Tenants"),
    _view(User, "users", "Users"),
    _view(Product, "products", "Products"),
    _view(Listing, "listings", "Listings"),
    _view(ListingVersion, "listing_versions", "Listing versions"),
    _view(ValidationRun, "validation_runs", "Validation runs"),
    _view(AgentRun, "agent_runs", "Agent runs"),
    _view(JobRun, "job_runs", "Job runs"),
    _view(Image, "images", "Images"),
    _view(Rule, "rules", "Rules"),
    _view(MarketplaceAccount, "marketplace_accounts", "Marketplace accounts"),
    _view(AuditLog, "audit_log", "Audit log"),
)


def view_identities() -> list[str]:
    return [view.identity for view in ADMIN_VIEWS]


def register_views(admin: Admin) -> None:
    for view in ADMIN_VIEWS:
        admin.add_view(view)
