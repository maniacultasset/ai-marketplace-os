"""The Home summary reads (client feedback 11 Nov 2026, item 6; `app.services.home`).

One small repository per root table, each built on its own `_select()` (the tenant criterion
and the guard stamp), with every joined or correlated tenant table tied to the root row's
`tenant_id` (RLS is the backstop). Every read is a fixed number of statements, whatever the
tenant's size: grouped counts, or a short page with its product joined in (no N+1).

The open-error predicates are the display state's (`app.services.listing_display`): an open
`error` row of a listing counts when it has no version (a listing-level answer) or sits on the
listing's current version; an older version's row is history. Such a row "waits on a question"
when its `human_task_id` names an open task (`listing_issues.blocking_task_ids`).

Maker-checker (`own_work`) walks each reviewable listing's contributor chain in one recursive
CTE, the same walk as `app.services.maker_checker.contributor_chain` and `lv_guard`: from the
current version back through `parent_version_id`, collecting `generated_by_user_id` and
`edited_by_user_id`, stopping after a `generation` or a full `regeneration` (`changed_paths`
NULL). `UNION` (not `UNION ALL`) ends the walk on a cycle.
"""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    case,
    false,
    func,
    literal,
    literal_column,
    not_,
    or_,
    select,
    text,
)

from app.db.models.catalog import Product
from app.db.models.catalog_assets import Image, MarketplaceAccount
from app.db.models.issues import HumanTask, ListingIssue
from app.db.models.listings import Listing, ListingVersion
from app.db.models.ops import JobRun
from app.repositories.base import TenantScopedRepository
from app.repositories.human_tasks import task_in_inbox
from app.repositories.listing_issues import issue_in_inbox

LIVE_STATES: Final = ("live", "partially_live")
NEEDS_YOU_STATUSES: Final = ("validated", "needs_review", "blocked")
STOPPED_STATUSES: Final = ("blocked", "failed")
REVIEWABLE: Final = ("validated", "needs_review")
FEED_KINDS: Final = (
    "listing_generate",
    "listing_publish",
    "bulk_import",
    "image_compliance",
    "brand_embed",
)
FIXED: Final = "auto_fixed"
AI_FIX: Final = "ai_correction"
READ_BUDGET: Final = "5s"


@dataclass(frozen=True, slots=True)
class BucketGroup:
    marketplace: str
    status: str
    marketplace_state: str
    has_error: bool
    question_only: bool
    n: int


@dataclass(frozen=True, slots=True)
class ListingRow:
    listing_id: uuid.UUID
    marketplace: str
    status: str
    status_reason: str | None
    marketplace_state: str
    current_version_id: uuid.UUID | None
    at: datetime
    product_id: uuid.UUID
    sku: str | None
    title: str | None


@dataclass(frozen=True, slots=True)
class IssueRow:
    issue_id: uuid.UUID
    listing_id: uuid.UUID
    error_code: str
    plain_message: str | None
    message: str
    open_count: int


@dataclass(frozen=True, slots=True)
class TaskRow:
    task_id: uuid.UUID
    kind: str
    question: str
    at: datetime
    product_id: uuid.UUID | None
    sku: str | None
    title: str | None


@dataclass(frozen=True, slots=True)
class JobRow:
    kind: str
    entity_type: str
    status: str
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    listing_id: uuid.UUID | None
    marketplace: str | None
    product_id: uuid.UUID | None
    title: str | None


@dataclass(frozen=True, slots=True)
class FixRow:
    error_code: str
    field_path: str
    resolved_at: datetime
    listing_id: uuid.UUID
    marketplace: str
    product_id: uuid.UUID | None
    title: str | None


OPEN: Final[ColumnElement[str]] = literal_column("'open'")
"""A SQL literal, never a bind: the open-row partial indexes must match under generic plans."""


def _waits_on_question() -> ColumnElement[bool]:
    """The open-error row's `human_task_id` names an open task."""
    return (
        select(literal(1))
        .where(
            HumanTask.tenant_id == ListingIssue.tenant_id,
            HumanTask.id == ListingIssue.human_task_id,
            HumanTask.status == OPEN,
        )
        .exists()
    )


def open_errors(*extra: ColumnElement[bool]) -> ColumnElement[bool]:
    """An open blocking row on the listing's current version (or with no version) exists."""
    return (
        select(literal(1))
        .where(
            ListingIssue.tenant_id == Listing.tenant_id,
            ListingIssue.listing_id == Listing.id,
            ListingIssue.status == OPEN,
            ListingIssue.severity == "error",
            or_(
                ListingIssue.listing_version_id.is_(None),
                ListingIssue.listing_version_id == Listing.current_version_id,
            ),
            *extra,
        )
        .exists()
    )


def question_only() -> ColumnElement[bool]:
    """Every open blocking row of the listing waits on an open question (and there is one)."""
    return and_(open_errors(_waits_on_question()), not_(open_errors(not_(_waits_on_question()))))


def problem() -> ColumnElement[bool]:
    """The `problem` bucket in SQL (`app.services.home.listing_bucket`, the same order)."""
    return and_(
        Listing.marketplace_state.not_in(LIVE_STATES),
        or_(
            Listing.marketplace_state == "rejected",
            and_(Listing.status.in_(NEEDS_YOU_STATUSES), open_errors()),
            Listing.status.in_(STOPPED_STATUSES),
        ),
    )


def ready_to_approve() -> builtins.list[ColumnElement[bool]]:
    """The `ready_to_approve` bucket in SQL: reviewable, no open blocking issue, neither live,
    partly live nor rejected."""
    return [
        Listing.marketplace_state.not_in((*LIVE_STATES, "rejected")),
        Listing.status.in_(REVIEWABLE),
        not_(open_errors()),
    ]


BUCKET_FILTERS: Final[dict[str, tuple[str, ...]]] = {
    "live": ("live",),
    "partly_live": ("partly_live",),
    "any_live": ("live", "partly_live"),
    "in_progress": ("in_progress",),
    "problem": ("problem",),
    "ready_to_approve": ("ready_to_approve",),
    "ready_to_publish": ("ready_to_publish",),
    "draft": ("draft",),
    "inactive": ("inactive",),
}
"""The list filters over the Home buckets (`GET /listings?bucket=`, `GET /products?listing=`):
one bucket each, and `any_live` for the Live tile, which counts live and partly live."""


def _word(value: str) -> ColumnElement[str]:
    return literal_column(f"'{value}'")


def listing_bucket_sql() -> ColumnElement[str]:
    """The bucket of a listing in SQL: `app.services.home.listing_bucket`, the same order, so a
    list filtered by it holds exactly the listings a Home count counts."""
    state, status = Listing.marketplace_state, Listing.status
    return case(
        (state == "live", _word("live")),
        (state == "partially_live", _word("partly_live")),
        (state == "rejected", _word("problem")),
        (and_(status.in_(NEEDS_YOU_STATUSES), open_errors()), _word("problem")),
        (status.in_(("generating", "publishing")), _word("in_progress")),
        (status.in_(REVIEWABLE), _word("ready_to_approve")),
        (status == "approved", _word("ready_to_publish")),
        (status.in_(STOPPED_STATUSES), _word("problem")),
        (and_(status == "published", state.in_(("inactive", "removed"))), _word("inactive")),
        (status == "published", _word("in_progress")),
        else_=_word("draft"),
    )


def in_bucket(name: str) -> ColumnElement[bool]:
    """The listing is in the `BUCKET_FILTERS[name]` buckets. An unknown name is a bug."""
    return listing_bucket_sql().in_([_word(b) for b in BUCKET_FILTERS[name]])


def _chain_root(origin: Any, changed_paths: Any) -> ColumnElement[bool]:
    """The version starts a chain (`maker_checker._starts_chain`)."""
    return or_(origin == "generation", and_(origin == "regeneration", changed_paths.is_(None)))


def _product_join() -> ColumnElement[bool]:
    return and_(Product.tenant_id == Listing.tenant_id, Product.id == Listing.product_id)


class HomeListingReads(TenantScopedRepository[Listing]):
    model = Listing

    async def bounded(self) -> None:
        """READ ONLY, with one statement and transaction cap (the S23 analytics budget)."""
        await self.db.execute(text("SET TRANSACTION READ ONLY"))
        await self.db.execute(text(f"SET LOCAL transaction_timeout = '{READ_BUDGET}'"))
        await self.db.execute(text(f"SET LOCAL statement_timeout = '{READ_BUDGET}'"))

    async def bucket_groups(self) -> builtins.list[BucketGroup]:
        """Every listing that is not archived, grouped by what its bucket depends on."""
        has_error = open_errors().label("has_error")
        only = question_only().label("question_only")
        stmt = (
            self._select()
            .where(Listing.archived_at.is_(None))
            .with_only_columns(
                Listing.marketplace,
                Listing.status,
                Listing.marketplace_state,
                has_error,
                only,
                func.count(),
            )
            # by output name: the EXISTS expressions (and their parameters) are not repeated
            .group_by(
                Listing.marketplace,
                Listing.status,
                Listing.marketplace_state,
                literal_column("has_error"),
                literal_column("question_only"),
            )
        )
        return [
            BucketGroup(str(m), str(s), str(ms), bool(e), bool(q), int(n))
            for m, s, ms, e, q, n in (await self.db.execute(stmt)).all()
        ]

    async def live_products(self) -> int:
        stmt = (
            self._select()
            .where(Listing.archived_at.is_(None), Listing.marketplace_state.in_(LIVE_STATES))
            .with_only_columns(func.count(func.distinct(Listing.product_id)))
        )
        return int((await self.db.execute(stmt)).scalar_one())

    async def rows(
        self,
        where: Sequence[ColumnElement[bool]],
        *,
        limit: int,
    ) -> builtins.list[ListingRow]:
        """Listings that are not archived matching `where`, newest status change first, each
        with its product's SKU and title."""
        stmt = (
            self._select()
            .join(Product, _product_join())
            .where(Listing.archived_at.is_(None), *where)
            .with_only_columns(
                Listing.id,
                Listing.marketplace,
                Listing.status,
                Listing.status_reason,
                Listing.marketplace_state,
                Listing.current_version_id,
                Listing.status_changed_at,
                Listing.product_id,
                Product.sku,
                Product.title,
            )
            .order_by(Listing.status_changed_at.desc(), Listing.id.desc())
            .limit(limit)
        )
        return [ListingRow(*row) for row in (await self.db.execute(stmt)).all()]

    def own_work_ids(self, user_id: uuid.UUID) -> Select[Any]:
        """The ids of the listings ready to approve whose contributor chain names `user_id`
        (module docstring): maker-checker refuses that user's approval."""
        v = ListingVersion
        anchor = (
            self._select()
            .join(v, and_(v.tenant_id == Listing.tenant_id, v.id == Listing.current_version_id))
            .where(Listing.archived_at.is_(None), *ready_to_approve())
            .with_only_columns(
                Listing.id.label("listing_id"),
                v.parent_version_id.label("parent_id"),
                _chain_root(v.origin, v.changed_paths).label("root"),
                v.generated_by_user_id.label("maker"),
                v.edited_by_user_id.label("editor"),
            )
            .cte("own_chain", recursive=True)
        )
        step = (
            select(
                anchor.c.listing_id,
                v.parent_version_id,
                _chain_root(v.origin, v.changed_paths),
                v.generated_by_user_id,
                v.edited_by_user_id,
            )
            .join_from(anchor, v, and_(v.tenant_id == self.tenant_id, v.id == anchor.c.parent_id))
            .where(anchor.c.root.is_(false()))
        )
        chain = anchor.union(step)
        return select(chain.c.listing_id).where(
            or_(chain.c.maker == user_id, chain.c.editor == user_id)
        )

    async def own_work(self, user_id: uuid.UUID) -> int:
        """How many listings ready to approve `user_id` made or edited (`own_work_ids`)."""
        stmt = (
            self._select()
            .where(Listing.id.in_(self.own_work_ids(user_id)))
            .with_only_columns(func.count())
        )
        return int((await self.db.execute(stmt)).scalar_one())

    async def blocked_rows(self, limit: int) -> builtins.list[ListingRow]:
        """Problem listings that wait on a fix (not only on an answer)."""
        return await self.rows([problem(), not_(question_only())], limit=limit)

    async def ready_rows(
        self,
        *,
        approve: bool,
        marketplaces: Sequence[str],
        limit: int,
        not_by: uuid.UUID | None = None,
    ) -> builtins.list[ListingRow]:
        """The `ready_to_approve` bucket (reviewable, no open blocking issue; without the ones
        `not_by` made or edited) or the `ready_to_publish` one (approved), on `marketplaces`,
        neither live nor rejected."""
        if not marketplaces:
            return []
        where: list[ColumnElement[bool]] = [Listing.marketplace.in_(list(marketplaces))]
        if approve:
            where += ready_to_approve()
            if not_by is not None:
                where.append(Listing.id.not_in(self.own_work_ids(not_by)))
        else:
            where += [
                Listing.marketplace_state.not_in((*LIVE_STATES, "rejected")),
                Listing.status == "approved",
            ]
        return await self.rows(where, limit=limit)


class HomeIssueReads(TenantScopedRepository[ListingIssue]):
    model = ListingIssue

    async def newest_open_errors(
        self, versions: Sequence[tuple[uuid.UUID, uuid.UUID | None]]
    ) -> dict[uuid.UUID, IssueRow]:
        """Per listing of `(listing_id, current_version_id)`: its newest open blocking row on
        the current version (or with no version) that waits on no open question (the Inbox
        shows a row that does only as its question), else its newest one; and how many such
        rows it has."""
        if not versions:
            return {}
        current = dict(versions)
        stmt = (
            self._select()
            .where(
                ListingIssue.listing_id.in_(list(current)),
                ListingIssue.status == "open",
                ListingIssue.severity == "error",
            )
            .with_only_columns(
                ListingIssue.id,
                ListingIssue.listing_id,
                ListingIssue.listing_version_id,
                ListingIssue.error_code,
                ListingIssue.plain_message,
                ListingIssue.message,
            )
            .order_by(
                _waits_on_question(), ListingIssue.last_seen_at.desc(), ListingIssue.id.desc()
            )
        )
        out: dict[uuid.UUID, IssueRow] = {}
        counts: dict[uuid.UUID, int] = {}
        for issue_id, listing_id, version_id, code, plain, message in (
            await self.db.execute(stmt)
        ).all():
            if version_id is not None and version_id != current.get(listing_id):
                continue
            counts[listing_id] = counts.get(listing_id, 0) + 1
            out.setdefault(listing_id, IssueRow(issue_id, listing_id, code, plain, message, 0))
        return {
            k: IssueRow(v.issue_id, k, v.error_code, v.plain_message, v.message, counts[k])
            for k, v in out.items()
        }

    async def inbox_counts(self) -> tuple[int, int]:
        """Client pass 2, B16: (open issues the Issues list shows, those of them that wait on no
        open question the Questions list shows). The Inbox's All view lists a question once
        and an issue on its own only when no open question stands for it."""
        question = (
            select(literal(1))
            .where(
                HumanTask.tenant_id == ListingIssue.tenant_id,
                HumanTask.id == ListingIssue.human_task_id,
                HumanTask.status == "open",
                task_in_inbox(),
            )
            .exists()
        )
        alone = case((question, 0), else_=1)
        stmt = (
            self._select()
            .where(ListingIssue.status == "open", issue_in_inbox())
            .with_only_columns(func.count(), func.coalesce(func.sum(alone), 0))
        )
        issues, without_question = (await self.db.execute(stmt)).one()
        return int(issues), int(without_question)

    async def recent_fixes(self, limit: int) -> builtins.list[FixRow]:
        """Rows Kuinn closed itself (a code fix, or an AI rewrite of the field), newest first,
        on listings that are not archived."""
        stmt = (
            self._select()
            .join(
                Listing,
                and_(
                    Listing.tenant_id == ListingIssue.tenant_id,
                    Listing.id == ListingIssue.listing_id,
                ),
            )
            .outerjoin(Product, _product_join())
            .where(
                Listing.archived_at.is_(None),
                ListingIssue.resolved_at.is_not(None),
                or_(ListingIssue.status == FIXED, ListingIssue.resolution == AI_FIX),
            )
            .with_only_columns(
                ListingIssue.error_code,
                ListingIssue.field_path,
                ListingIssue.resolved_at,
                ListingIssue.listing_id,
                ListingIssue.marketplace,
                Listing.product_id,
                Product.title,
            )
            .order_by(ListingIssue.resolved_at.desc(), ListingIssue.id.desc())
            .limit(limit)
        )
        return [FixRow(*row) for row in (await self.db.execute(stmt)).all()]


def _blocking() -> ColumnElement[bool]:
    """`TaskOut.blocking`: an open ERROR row is linked to the task."""
    return (
        select(literal(1))
        .where(
            ListingIssue.tenant_id == HumanTask.tenant_id,
            ListingIssue.human_task_id == HumanTask.id,
            ListingIssue.status == OPEN,
            ListingIssue.severity == "error",
        )
        .exists()
    )


class HomeTaskReads(TenantScopedRepository[HumanTask]):
    model = HumanTask

    async def open_counts(self) -> tuple[int, int]:
        """(open tasks that block a listing, open tasks that do not), over the Questions list's
        rows (`task_in_inbox`): the two add up to the Inbox's question count."""
        blocking = _blocking().label("blocking")
        stmt = (
            self._select()
            .where(HumanTask.status == "open", task_in_inbox())
            .with_only_columns(blocking, func.count())
            .group_by(literal_column("blocking"))
        )
        found = {bool(b): int(n) for b, n in (await self.db.execute(stmt)).all()}
        return found.get(True, 0), found.get(False, 0)

    async def blocking_rows(self, limit: int) -> builtins.list[TaskRow]:
        stmt = (
            self._select()
            .outerjoin(
                Product,
                and_(Product.tenant_id == HumanTask.tenant_id, Product.id == HumanTask.product_id),
            )
            .where(HumanTask.status == "open", task_in_inbox(), _blocking())
            .with_only_columns(
                HumanTask.id,
                HumanTask.kind,
                HumanTask.question,
                HumanTask.created_at,
                HumanTask.product_id,
                Product.sku,
                Product.title,
            )
            .order_by(HumanTask.created_at.desc(), HumanTask.id.desc())
            .limit(limit)
        )
        return [TaskRow(*row) for row in (await self.db.execute(stmt)).all()]


class HomeJobReads(TenantScopedRepository[JobRun]):
    model = JobRun

    async def recent(self, limit: int) -> builtins.list[JobRow]:
        """The newest jobs of the feed's kinds (by `queued_at`, on `ix_job_runs_recent`), with
        the product of their listing or photo. Cancelled jobs, status polls and the jobs of
        archived listings or deleted products are left out."""
        image_join = and_(
            Image.tenant_id == JobRun.tenant_id,
            JobRun.entity_type == "image",
            Image.id == JobRun.entity_id,
        )
        listing_join = and_(Listing.tenant_id == JobRun.tenant_id, Listing.id == JobRun.listing_id)
        product_join = and_(
            Product.tenant_id == JobRun.tenant_id,
            Product.id == func.coalesce(Listing.product_id, Image.product_id),
        )
        stmt = (
            self._select()
            .outerjoin(Listing, listing_join)
            .outerjoin(Image, image_join)
            .outerjoin(Product, product_join)
            .where(
                JobRun.kind.in_(FEED_KINDS),
                JobRun.status != "cancelled",
                Listing.archived_at.is_(None),
                Product.deleted_at.is_(None),
                # a photo job without its photo row (a candidate, a size-chart read) is not one
                or_(JobRun.kind != "image_compliance", Image.id.is_not(None)),
            )
            .with_only_columns(
                JobRun.kind,
                JobRun.entity_type,
                JobRun.status,
                JobRun.queued_at,
                JobRun.started_at,
                JobRun.finished_at,
                JobRun.listing_id,
                Listing.marketplace,
                Product.id,
                Product.title,
            )
            .order_by(JobRun.queued_at.desc(), JobRun.id.desc())
            .limit(limit)
        )
        return [JobRow(*row) for row in (await self.db.execute(stmt)).all()]


class HomeProductReads(TenantScopedRepository[Product]):
    model = Product

    async def count(self) -> int:
        """Styles and standalones that are not deleted (the Products list's rows)."""
        stmt = (
            self._select()
            .where(Product.deleted_at.is_(None), Product.parent_product_id.is_(None))
            .with_only_columns(func.count())
        )
        return int((await self.db.execute(stmt)).scalar_one())


class HomeAccountReads(TenantScopedRepository[MarketplaceAccount]):
    model = MarketplaceAccount

    async def current(self) -> dict[str, str]:
        """marketplace -> the status of its account that is not revoked (at most one each)."""
        stmt = (
            self._select()
            .where(MarketplaceAccount.status != "revoked")
            .with_only_columns(MarketplaceAccount.marketplace, MarketplaceAccount.status)
        )
        return {str(m): str(s) for m, s in (await self.db.execute(stmt)).all()}


@dataclass(frozen=True, slots=True)
class HomeReads:
    listings: HomeListingReads
    issues: HomeIssueReads
    tasks: HomeTaskReads
    jobs: HomeJobReads
    products: HomeProductReads
    accounts: HomeAccountReads


def home_reads(session: Any, tenant_id: uuid.UUID) -> HomeReads:
    return HomeReads(
        HomeListingReads(session, tenant_id),
        HomeIssueReads(session, tenant_id),
        HomeTaskReads(session, tenant_id),
        HomeJobReads(session, tenant_id),
        HomeProductReads(session, tenant_id),
        HomeAccountReads(session, tenant_id),
    )
