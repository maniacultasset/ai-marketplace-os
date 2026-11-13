"""FB6 (client feedback 11 Nov 2026, item 6): the Home summary's pure parts. Every listing lands in
one bucket, the caller's work follows the role and the publish switch, and every line reads in
plain words."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app.repositories.home import BucketGroup, FixRow, IssueRow, JobRow, ListingRow, TaskRow
from app.services.home import (
    ACTIVITY_MAX,
    BUCKETS,
    ITEMS_MAX,
    Permissions,
    SendState,
    Tally,
    activity,
    blocked_issue_text,
    fix_activity,
    job_activity,
    listing_bucket,
    marketplace_rows,
    needs_you_counts,
    needs_you_items,
    next_step,
    send_state,
    tally,
)

T0 = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
STATUSES = (
    "created", "generating", "validated", "needs_review", "approved", "publishing", "published",
    "blocked", "failed",
)  # fmt: skip
STATES = (
    "not_submitted", "processing", "pending_review", "live", "partially_live", "inactive",
    "rejected", "removed",
)  # fmt: skip


@pytest.mark.parametrize(
    ("status", "state", "has_error", "bucket"),
    [
        ("published", "live", False, "live"),
        ("approved", "live", True, "live"),  # an approved update of a live listing stays live
        ("published", "partially_live", False, "partly_live"),
        ("published", "rejected", False, "problem"),
        ("validated", "not_submitted", True, "problem"),
        ("needs_review", "not_submitted", True, "problem"),
        ("blocked", "not_submitted", False, "problem"),
        ("failed", "not_submitted", False, "problem"),
        ("generating", "not_submitted", False, "in_progress"),
        ("publishing", "not_submitted", False, "in_progress"),
        ("published", "processing", False, "in_progress"),
        ("published", "pending_review", False, "in_progress"),
        ("validated", "not_submitted", False, "ready_to_approve"),
        ("needs_review", "not_submitted", False, "ready_to_approve"),
        ("approved", "not_submitted", False, "ready_to_publish"),
        ("approved", "not_submitted", True, "ready_to_publish"),  # approved: not a person's fix
        ("published", "inactive", False, "inactive"),
        ("published", "removed", False, "inactive"),
        ("created", "not_submitted", False, "draft"),
    ],
)
def test_listing_bucket(status: str, state: str, has_error: bool, bucket: str) -> None:
    assert listing_bucket(status, state, has_error) == bucket


def test_every_listing_lands_in_one_known_bucket() -> None:
    for status in STATUSES:
        for state in STATES:
            for err in (False, True):
                assert listing_bucket(status, state, err) in BUCKETS


def _g(
    m: str,
    status: str,
    state: str = "not_submitted",
    *,
    err: bool = False,
    q: bool = False,
    n: int = 1,
) -> BucketGroup:
    return BucketGroup(m, status, state, err, q, n)


def test_tally_sums_to_total_and_splits_blocked_from_questions() -> None:
    whole, per = tally(
        [
            _g("amazon", "published", "live", n=3),
            _g("amazon", "blocked", err=True, n=2),  # waits on a fix
            _g("amazon", "blocked", err=True, q=True),  # waits on an answer only
            _g("flipkart", "failed"),
            _g("flipkart", "validated", n=4),
            _g("flipkart", "approved", n=2),
        ]
    )
    assert whole.total == 13 == sum(t.total for t in per.values())
    assert whole.counts["problem"] == 4 and whole.blocked == 3
    assert per["amazon"].blocked == 2 and per["flipkart"].blocked == 1
    assert per["flipkart"].counts["ready_to_approve"] == 4
    # the failed listing has no open blocking issue: no Inbox row for it
    assert whole.blocked_without_issue == 1 and per["flipkart"].blocked_without_issue == 1


def test_tally_keeps_both_marketplaces_when_empty() -> None:
    whole, per = tally([])
    assert whole.total == 0 and set(per) == {"amazon", "flipkart"}


def _per() -> dict[str, Tally]:
    _, per = tally(
        [
            _g("amazon", "blocked", err=True),
            _g("amazon", "failed"),
            _g("amazon", "validated", n=2),
            _g("amazon", "approved", n=3),
            _g("flipkart", "approved"),
        ]
    )
    return per


OPEN_FLIPKART: dict[str, SendState] = {"amazon": "paused", "flipkart": "open"}


@pytest.mark.parametrize(
    ("role", "fix", "answer", "approve", "publish", "paused"),
    [
        ("owner", 2, 4, 2, 1, 3),
        ("reviewer", 2, 0, 2, 0, 3),  # rewrites and approves; answering needs product:import
        ("operator", 2, 4, 0, 1, 3),
        ("admin", 0, 0, 0, 0, 0),  # acts on no listing: nothing needs it
        ("unknown", 0, 0, 0, 0, 0),
    ],
)
def test_needs_you_follows_role_and_switch(
    role: str, fix: int, answer: int, approve: int, publish: int, paused: int
) -> None:
    counts = needs_you_counts(_per(), 4, 2, Permissions.of(role), OPEN_FLIPKART)
    assert counts["blocked"] == fix and counts["questions"] == answer
    assert counts["blocked_without_issue"] == (1 if fix else 0)
    assert counts["improve"] == (2 if answer else 0)
    assert counts["ready_to_approve"] == approve
    assert counts["ready_to_publish"] == publish
    assert counts["publishing_paused"] == paused
    assert counts["waiting_on_account"] == 0
    assert counts["total"] == fix + answer + approve + publish


def test_needs_you_counts_every_approved_listing_while_publishing_is_on() -> None:
    sending: dict[str, SendState] = {"amazon": "open", "flipkart": "open"}
    counts = needs_you_counts(_per(), 0, 0, Permissions.of("owner"), sending)
    assert counts["ready_to_publish"] == 4 and counts["publishing_paused"] == 0


def test_needs_you_keeps_own_work_out_of_approve() -> None:
    owner = Permissions.of("owner")
    counts = needs_you_counts(_per(), 0, 0, owner, OPEN_FLIPKART, own_work=2)
    assert counts["ready_to_approve"] == 0 and counts["own_work"] == 2
    assert counts["total"] == 2 + 1  # blocked + the open Flipkart listing
    some = needs_you_counts(_per(), 0, 0, owner, OPEN_FLIPKART, own_work=1)
    assert some["ready_to_approve"] == 1 and some["own_work"] == 1
    operator = needs_you_counts(_per(), 0, 0, Permissions.of("operator"), OPEN_FLIPKART, 2)
    assert operator["own_work"] == 0  # cannot approve anyway


def test_needs_you_waits_on_the_account() -> None:
    sending: dict[str, SendState] = {"amazon": "account", "flipkart": "open"}
    counts = needs_you_counts(_per(), 0, 0, Permissions.of("operator"), sending)
    assert counts["ready_to_publish"] == 1 and counts["waiting_on_account"] == 3
    assert counts["publishing_paused"] == 0
    reviewer = needs_you_counts(_per(), 0, 0, Permissions.of("reviewer"), sending)
    assert reviewer["ready_to_publish"] == 0 and reviewer["waiting_on_account"] == 3


@pytest.mark.parametrize(
    ("publishing", "account", "state"),
    [
        (False, "active", "paused"),
        (False, None, "paused"),  # paused wins: nothing to connect for yet
        (True, None, "account"),
        (True, "needs_reconnect", "account"),
        (True, "active", "open"),
    ],
)
def test_send_state(publishing: bool, account: str | None, state: str) -> None:
    assert send_state(publishing, account) == state


@pytest.mark.parametrize(
    ("role", "answer", "fix", "approve", "publish"),
    [
        ("owner", True, True, True, True),
        ("operator", True, True, False, True),
        ("reviewer", False, True, True, False),
        ("admin", False, False, False, False),
    ],
)
def test_permissions_follow_the_matrix(
    role: str, answer: bool, fix: bool, approve: bool, publish: bool
) -> None:
    assert Permissions.of(role) == Permissions(answer, fix, approve, publish)


def _row(
    status: str = "blocked",
    reason: str | None = None,
    state: str = "not_submitted",
    at: datetime = T0,
    market: str = "amazon",
) -> ListingRow:
    return ListingRow(
        uuid.uuid4(), market, status, reason, state, uuid.uuid4(), at, uuid.uuid4(), "SKU-1",
        "Kurta",
    )  # fmt: skip


def _issue(row: ListingRow, plain: str | None = "Shorten the title.") -> IssueRow:
    return IssueRow(
        uuid.uuid4(), row.listing_id, "FIELD_TOO_LONG", plain, "content.title: too long", 2
    )


@pytest.mark.parametrize(
    ("row", "with_issue", "plain", "text"),
    [
        (_row(), True, "Shorten the title.", "Shorten the title."),
        (_row(), True, None, "content.title: too long"),
        (_row(state="rejected"), False, None, "The marketplace rejected this listing."),
        (_row(reason="human_task_open"), False, None, "A question is waiting for your answer."),
        (
            _row(status="failed", reason="publish_failed"),
            False,
            None,
            "Sending it to the marketplace failed.",
        ),
        (_row(status="failed"), False, None, "The last step failed."),
        (_row(reason="something_new"), False, None, "Checks stopped this listing."),
    ],
)
def test_blocked_issue_text(
    row: ListingRow, with_issue: bool, plain: str | None, text: str
) -> None:
    issue = _issue(row, plain) if with_issue else None
    assert blocked_issue_text(row, issue) == text


def test_issue_text_is_clipped_and_one_line() -> None:
    row = _row()
    long = IssueRow(uuid.uuid4(), row.listing_id, "X", "word\n" * 200, "m", 1)
    text = blocked_issue_text(row, long)
    assert len(text) <= 500 and "\n" not in text and text.endswith("…")


def test_needs_you_items_order_words_and_cap() -> None:
    blocked = [_row()]
    issues = {blocked[0].listing_id: _issue(blocked[0])}
    task = TaskRow(uuid.uuid4(), "missing_fact", "What is the fabric?", T0, uuid.uuid4(), "S", "T")
    approve = [_row("validated")]
    publish = [_row("approved", market="flipkart"), _row("approved"), _row("approved")]
    items = needs_you_items(blocked, issues, [task], approve, publish)
    assert [i["kind"] for i in items] == ["blocked", "question", "approve", "publish", "publish"]
    assert len(items) == ITEMS_MAX
    first, question, ready, send = items[0], items[1], items[2], items[3]
    assert first["action"] == "fix" and first["issue_count"] == 2 and first["issue_id"]
    assert question["action"] == "answer" and question["task_id"] == task.task_id
    assert question["issue"] == "What is the fabric?" and question.get("listing_id") is None
    assert ready["action"] == "approve" and ready["issue"].startswith("Written and checked")
    assert send["issue"] == "Approved. Ready to publish to Flipkart."


def test_blocked_item_without_an_issue() -> None:
    row = _row(status="failed", reason="generation_failed")
    [item] = needs_you_items([row], {}, [], [], [])
    assert item["issue_id"] is None and item["issue_count"] == 0
    assert item["issue"] == "Writing the listing failed."


def _job(
    kind: str, status: str, *, market: str | None = "amazon", title: str | None = "Kurta"
) -> JobRow:
    return JobRow(
        kind, "listing", status, T0, T0 + timedelta(seconds=5), None, None, market, None, title
    )


@pytest.mark.parametrize(
    ("kind", "status", "state", "text"),
    [
        ("listing_generate", "queued", "pending", "Writing the Amazon listing for Kurta"),
        ("listing_generate", "running", "pending", "Writing the Amazon listing for Kurta"),
        ("listing_generate", "succeeded", "completed", "Wrote the Amazon listing for Kurta"),
        ("listing_generate", "partial", "completed", "Wrote the Amazon listing for Kurta"),
        ("listing_generate", "failed", "failed", "Could not write the Amazon listing for Kurta"),
        (
            "listing_generate",
            "dead_letter",
            "failed",
            "Could not write the Amazon listing for Kurta",
        ),
        ("listing_publish", "succeeded", "completed", "Sent Kurta to Amazon"),
        ("listing_publish", "failed", "failed", "Could not send Kurta to Amazon"),
        ("image_compliance", "running", "pending", "Checking a photo of Kurta"),
        ("bulk_import", "succeeded", "completed", "Imported your spreadsheet"),
        ("brand_embed", "failed", "failed", "Could not read your brand material"),
    ],
)
def test_job_activity_words(kind: str, status: str, state: str, text: str) -> None:
    item = job_activity(_job(kind, status))
    assert item is not None
    assert item["state"] == state and item["text"] == text
    assert item["at"] == T0 + timedelta(seconds=5)  # started, not finished yet


def test_job_activity_skips_unknown_and_cancelled() -> None:
    assert job_activity(_job("listing_status_sync", "succeeded")) is None
    assert job_activity(_job("listing_generate", "cancelled")) is None


def test_job_activity_without_marketplace_or_title() -> None:
    item = job_activity(_job("listing_generate", "succeeded", market=None, title=None))
    assert item is not None and item["text"] == "Wrote the listing for a product"


@pytest.mark.parametrize(
    ("path", "words"),
    [
        ("content.title", "the title"),
        ("content.bullets.2", "a bullet point"),
        ("content.description", "the description"),
        ("content.search_terms", "the search terms"),
        ("attributes.color", "an attribute"),
        ("images.main", "a photo"),
        ("other", "a field"),
    ],
)
def test_fix_activity_words(path: str, words: str) -> None:
    fix = FixRow("X", path, T0, uuid.uuid4(), "flipkart", uuid.uuid4(), "Kurta")
    item = fix_activity(fix)
    assert item["text"] == f"Fixed {words} of the Flipkart listing for Kurta"
    assert item["kind"] == "fix" and item["state"] == "completed"


def test_activity_newest_first_and_capped() -> None:
    jobs = [
        JobRow(
            "listing_generate",
            "listing",
            "succeeded",
            T0,
            None,
            T0 + timedelta(minutes=i),
            None,
            "amazon",
            None,
            "K",
        )
        for i in range(10)
    ]
    fixes = [
        FixRow("X", "content.title", T0 + timedelta(hours=1), uuid.uuid4(), "amazon", None, "K")
    ]
    items = activity(jobs, fixes)
    assert len(items) == ACTIVITY_MAX
    assert items[0]["kind"] == "fix"
    ats = [i["at"] for i in items]
    assert ats == sorted(ats, reverse=True)


def test_next_step() -> None:
    empty = Tally()
    busy = Tally()
    busy.add("live", 1, question_only=False)
    stuck = Tally()
    stuck.add("problem", 1, question_only=True)
    assert next_step(None, busy) == "connect"
    assert next_step("needs_reconnect", stuck) == "reconnect"
    assert next_step("active", stuck) == "fix"
    assert next_step("active", empty) == "add_listing"
    assert next_step("active", busy) == "none"


def test_marketplace_rows_follow_the_switches() -> None:
    settings = SimpleNamespace(flipkart_enabled=False, amazon_publish_enabled=False)
    rows = marketplace_rows({"amazon": "needs_reconnect"}, {}, settings)
    amazon, flipkart = rows
    assert amazon == {
        "marketplace": "amazon",
        "enabled": True,
        "account": "needs_reconnect",
        "publishing_enabled": False,
        "next_step": "reconnect",
    }
    assert flipkart["enabled"] is False and flipkart["account"] == "not_connected"
    assert flipkart["next_step"] == "connect"
    assert marketplace_rows({"amazon": "active"}, {}, settings)[0]["account"] == "connected"


# ---- item 6: the bucket filters; item 18: the cross-brand overview ----


def test_bucket_filters_are_the_wire_values_and_cover_every_bucket() -> None:
    from typing import get_args

    from app.api.v1.schemas.home import ListingBucketFilter
    from app.repositories.home import BUCKET_FILTERS

    assert set(BUCKET_FILTERS) == set(get_args(ListingBucketFilter))
    singles = {k: v for k, v in BUCKET_FILTERS.items() if k != "any_live"}
    assert all(v == (k,) for k, v in singles.items())
    assert set(singles) == set(BUCKETS)
    assert BUCKET_FILTERS["any_live"] == ("live", "partly_live")


def test_bucket_sql_names_every_bucket() -> None:
    from app.repositories.home import in_bucket, listing_bucket_sql
    from sqlalchemy.dialects import postgresql

    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    sql = str(listing_bucket_sql().compile(dialect=dialect))
    assert sql.startswith("CASE WHEN")
    for bucket in BUCKETS:
        assert f"'{bucket}'" in sql, bucket
    assert "ELSE 'draft'" in sql
    with pytest.raises(KeyError):
        in_bucket("everything")


def _member(name: str, role: str = "owner", tenant: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(tenant_id=tenant or uuid.uuid4(), tenant_name=name, role=role)


def test_brands_by_name_whatever_was_used_last() -> None:
    from app.services.home import brand_order

    rows = [_member("kuinn kids"), _member("Bharat"), _member("acme")]
    assert [m.tenant_name for m in brand_order(rows)] == ["acme", "Bharat", "kuinn kids"]


def _counted(**overrides: object) -> object:
    from app.services.home import Counted

    whole, per = tally(
        [
            BucketGroup("amazon", "validated", "not_submitted", False, False, 3),
            BucketGroup("amazon", "blocked", "not_submitted", True, False, 2),
            BucketGroup("flipkart", "approved", "not_submitted", False, False, 1),
        ]
    )
    fields: dict[str, object] = {
        "accounts": {"flipkart": "active"},
        "sending": {"amazon": "paused", "flipkart": "open"},
        "whole": whole,
        "per": per,
        "live_products": 0,
        "own_work": 1,
        "questions": 4,
        "improve": 0,
        "products": 6,
    }
    return Counted(**{**fields, **overrides})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("role", "needs"),
    [
        ("owner", 2 + 4 + 2 + 1),  # fixes, answers, approves (not its own one) and publishes
        ("reviewer", 2 + 2),  # rewrites and approves
        ("admin", 0),
    ],
)
def test_brand_row_counts_the_callers_role_there(role: str, needs: int) -> None:
    from app.services.home import brand_row

    m = _member("Bharat", role)
    row = brand_row(m, uuid.uuid4(), _counted())  # type: ignore[arg-type]
    assert row["tenant_id"] == m.tenant_id and row["name"] == "Bharat" and row["role"] == role
    assert row["current"] is False and row["products"] == 6
    assert row["listings"]["total"] == 6 and row["listings"]["problem"] == 2
    assert row["listings"]["live_products"] == 0
    assert row["needs_you"] == needs


def test_brand_row_marks_the_current_brand() -> None:
    from app.services.home import brand_row

    m = _member("Acme")
    assert brand_row(m, m.tenant_id, _counted())["current"] is True  # type: ignore[arg-type]


def test_inbox_counts_add_up() -> None:
    """Client pass 2, B16: Home 18 (2 blocked, 16 questions), Inbox 21, Questions 19 and Issues
    24 are four different things, each with its own name and one definition."""
    from app.services.home import inbox_counts

    got = inbox_counts(blocking=16, improve=3, issues=24, alone=2)
    assert got == {
        "open_items": 21,
        "questions": 19,
        "blocking_questions": 16,
        "issues": 24,
        "issues_without_question": 2,
    }
    assert inbox_counts(0, 0, 0, 0)["open_items"] == 0


def test_inbox_filters_keep_their_table_when_the_caller_joins_it() -> None:
    """Home's blocking rows join products: task_in_inbox's subquery must still read products."""
    from app.db.models.catalog import Product
    from app.db.models.issues import HumanTask, ListingIssue
    from app.db.models.listings import Listing
    from app.repositories.human_tasks import task_in_inbox
    from app.repositories.listing_issues import issue_in_inbox
    from sqlalchemy import and_, select
    from sqlalchemy.dialects import postgresql

    pg = postgresql.dialect()  # type: ignore[no-untyped-call]
    on = and_(Product.tenant_id == HumanTask.tenant_id, Product.id == HumanTask.product_id)
    tasks = select(HumanTask.id, Product.sku).outerjoin(Product, on).where(task_in_inbox())
    sql = str(tasks.compile(dialect=pg))
    assert sql.count("FROM products") == 1 and "JOIN products" in sql
    joined = and_(
        Listing.tenant_id == ListingIssue.tenant_id, Listing.id == ListingIssue.listing_id
    )
    issues = select(ListingIssue.id).join(Listing, joined).where(issue_in_inbox())
    assert "FROM listings" in str(issues.compile(dialect=pg))
