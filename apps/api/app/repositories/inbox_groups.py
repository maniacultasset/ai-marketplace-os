"""The grouped Inbox reads (G1; `app.services.inbox_groups`).

One small repository per root table, each built on its own `_select()` (the tenant criterion and
the guard stamp), with every joined tenant table tied to the root row's `tenant_id` (RLS is the
backstop), as the Home reads are. The rows are the Inbox's own: open tasks `task_in_inbox`, open
issues `issue_in_inbox`, and for the All view only the issues that wait on no open question
(`waits_on_open_question`), the predicates Home's `inbox` counts use (B16), so the groups add up
to those counts. A fixed number of statements, whatever the Inbox's size.
"""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, exists, func, or_, select, text

from app.db.models.catalog import Product
from app.db.models.issues import HumanTask, ListingIssue
from app.db.models.listings import Listing
from app.repositories.base import TenantScopedRepository
from app.repositories.home import READ_BUDGET, _blocking
from app.repositories.human_tasks import task_in_inbox
from app.repositories.listing_issues import issue_in_inbox, waits_on_open_question


@dataclass(frozen=True, slots=True)
class TaskRow:
    task: HumanTask
    sku: str | None
    title: str | None
    category: str | None
    parent_product_id: uuid.UUID | None
    blocking: bool


@dataclass(frozen=True, slots=True)
class IssueRow:
    issue: ListingIssue
    product_id: uuid.UUID | None
    sku: str | None
    title: str | None
    category: str | None


@dataclass(frozen=True, slots=True)
class ListingRef:
    id: uuid.UUID
    product_id: uuid.UUID
    marketplace: str
    status: str
    paused: bool
    locked_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProductFactsRow:
    id: uuid.UUID
    parent_product_id: uuid.UUID | None
    variation_theme: str | None
    attributes: dict[str, Any]
    gender: str | None
    brand_name: str | None
    hsn_code: str | None
    country_of_origin: str | None
    manufacturer_party_id: uuid.UUID | None
    marketer_party_id: uuid.UUID | None
    packer_party_id: uuid.UUID | None
    importer_party_id: uuid.UUID | None
    identifier_status: str | None


def _task_product() -> Any:
    return and_(Product.tenant_id == HumanTask.tenant_id, Product.id == HumanTask.product_id)


class InboxTaskReads(TenantScopedRepository[HumanTask]):
    model = HumanTask

    async def bounded(self) -> None:
        """READ ONLY, with one statement and transaction cap (the Home reads' 5 s budget)."""
        await self.db.execute(text("SET TRANSACTION READ ONLY"))
        await self.db.execute(text(f"SET LOCAL transaction_timeout = '{READ_BUDGET}'"))
        await self.db.execute(text(f"SET LOCAL statement_timeout = '{READ_BUDGET}'"))

    async def open_rows(self) -> builtins.list[TaskRow]:
        """Every open task the Questions list shows, with its product and whether an open
        listing ERROR waits on it."""
        stmt = (
            self._select()
            .outerjoin(Product, _task_product())
            .where(HumanTask.status == "open", task_in_inbox())
            .add_columns(
                Product.sku,
                Product.title,
                Product.category,
                Product.parent_product_id,
                _blocking().label("blocking"),
            )
            .order_by(HumanTask.created_at.desc(), HumanTask.id.desc())
        )
        return [TaskRow(*row) for row in (await self.db.execute(stmt)).all()]

    async def by_ids(self, task_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, HumanTask]:
        """These tasks of this tenant, whatever their status (another tenant's read as
        missing). Not locked: the answer locks each product, then its task."""
        if not task_ids:
            return {}
        stmt = self._select().where(HumanTask.id.in_(sorted(set(task_ids))))
        return {row.id: row for row in (await self.db.execute(stmt)).scalars()}


class InboxIssueReads(TenantScopedRepository[ListingIssue]):
    model = ListingIssue

    async def open_rows(self, *, without_question: bool) -> builtins.list[IssueRow]:
        """Every open issue the Issues list shows (`without_question`: only those that wait on
        no open question, the All view's), with its listing's product."""
        listing = and_(
            Listing.tenant_id == ListingIssue.tenant_id, Listing.id == ListingIssue.listing_id
        )
        product = and_(Product.tenant_id == Listing.tenant_id, Product.id == Listing.product_id)
        stmt = (
            self._select()
            .outerjoin(Listing, listing)
            .outerjoin(Product, product)
            .where(ListingIssue.status == "open", issue_in_inbox())
            .add_columns(Listing.product_id, Product.sku, Product.title, Product.category)
            .order_by(ListingIssue.created_at.desc(), ListingIssue.id.desc())
        )
        if without_question:
            stmt = stmt.where(~waits_on_open_question())
        return [IssueRow(*row) for row in (await self.db.execute(stmt)).all()]


class InboxListingReads(TenantScopedRepository[Listing]):
    model = Listing

    async def for_products(self, product_ids: Sequence[uuid.UUID]) -> builtins.list[ListingRef]:
        """The listings of these products that are not archived."""
        if not product_ids:
            return []
        stmt = (
            self._select()
            .where(Listing.product_id.in_(sorted(set(product_ids))), Listing.archived_at.is_(None))
            .with_only_columns(
                Listing.id,
                Listing.product_id,
                Listing.marketplace,
                Listing.status,
                Listing.paused_at.is_not(None),
                Listing.locked_paths,
            )
            .order_by(Listing.product_id, Listing.marketplace, Listing.id)
        )
        return [
            ListingRef(lid, product_id, marketplace, status, paused, tuple(locked or ()))
            for lid, product_id, marketplace, status, paused, locked in (
                await self.db.execute(stmt)
            ).all()
        ]

    async def with_open_targets(
        self, targets: Sequence[str], limit: int
    ) -> builtins.list[uuid.UUID]:
        """G1: the listings (not archived, not being written) of the products with an open task
        on one of `targets`, and of their styles; at most `limit`, oldest first."""
        product = and_(Product.tenant_id == HumanTask.tenant_id, Product.id == HumanTask.product_id)
        asking = exists(
            select(HumanTask.id)
            .join(Product, product)
            .where(
                HumanTask.tenant_id == Listing.tenant_id,
                HumanTask.status == "open",
                HumanTask.target_path.in_(list(targets)),
                or_(
                    Product.id == Listing.product_id,
                    Product.parent_product_id == Listing.product_id,
                ),
            )
        )
        stmt = (
            self._select()
            .where(
                Listing.archived_at.is_(None),
                Listing.status.not_in(("created", "generating")),
                asking,
            )
            .with_only_columns(Listing.id)
            .order_by(Listing.created_at, Listing.id)
            .limit(limit)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def with_open_questions(
        self, listing_ids: Sequence[uuid.UUID], kinds: Sequence[str]
    ) -> set[uuid.UUID]:
        """The listings among these on which a live (`open`/`answered`) task of `kinds` still
        stands: one on the listing's product or on a size of it, or one linked to the listing's
        refusals (`listing_issues.live_task_ids`, the check `services.tasks.fact_edits` makes)."""
        if not listing_ids:
            return set()
        live = (HumanTask.status.in_(("open", "answered")), HumanTask.kind.in_(list(kinds)))
        on_product = exists(
            select(HumanTask.id)
            .join(
                Product,
                and_(Product.tenant_id == HumanTask.tenant_id, Product.id == HumanTask.product_id),
            )
            .where(
                HumanTask.tenant_id == Listing.tenant_id,
                or_(
                    Product.id == Listing.product_id,
                    Product.parent_product_id == Listing.product_id,
                ),
                *live,
            )
        )
        linked = exists(
            select(ListingIssue.id)
            .join(
                HumanTask,
                and_(
                    HumanTask.tenant_id == ListingIssue.tenant_id,
                    HumanTask.id == ListingIssue.human_task_id,
                ),
            )
            .where(
                ListingIssue.tenant_id == Listing.tenant_id,
                ListingIssue.listing_id == Listing.id,
                *live,
            )
        )
        stmt = (
            self._select()
            .where(Listing.id.in_(sorted(set(listing_ids))), or_(on_product, linked))
            .with_only_columns(Listing.id)
        )
        return set((await self.db.execute(stmt)).scalars())


class InboxProductReads(TenantScopedRepository[Product]):
    model = Product

    async def facts(self, product_ids: Sequence[uuid.UUID]) -> builtins.list[ProductFactsRow]:
        """The answerable facts of these products (deleted ones included: the caller decides)."""
        if not product_ids:
            return []
        stmt = (
            self._select()
            .where(Product.id.in_(sorted(set(product_ids))))
            .with_only_columns(
                Product.id,
                Product.parent_product_id,
                Product.variation_theme,
                Product.attributes,
                Product.gender,
                Product.brand_name,
                Product.hsn_code,
                Product.country_of_origin,
                Product.manufacturer_party_id,
                Product.marketer_party_id,
                Product.packer_party_id,
                Product.importer_party_id,
                Product.identifier_status,
            )
        )
        return [ProductFactsRow(*row) for row in (await self.db.execute(stmt)).all()]

    async def variants_without_status(self, style_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        """style id -> how many of its live variants have no barcode and no exemption."""
        if not style_ids:
            return {}
        stmt = (
            self._select()
            .where(
                Product.parent_product_id.in_(sorted(set(style_ids))),
                Product.deleted_at.is_(None),
                Product.identifier_status.is_(None),
            )
            .with_only_columns(Product.parent_product_id, func.count())
            .group_by(Product.parent_product_id)
        )
        return {row[0]: int(row[1]) for row in (await self.db.execute(stmt)).all()}


@dataclass(frozen=True, slots=True)
class InboxGroupReads:
    tasks: InboxTaskReads
    issues: InboxIssueReads
    listings: InboxListingReads
    products: InboxProductReads


def inbox_group_reads(session: Any, tenant_id: uuid.UUID) -> InboxGroupReads:
    return InboxGroupReads(
        InboxTaskReads(session, tenant_id),
        InboxIssueReads(session, tenant_id),
        InboxListingReads(session, tenant_id),
        InboxProductReads(session, tenant_id),
    )
