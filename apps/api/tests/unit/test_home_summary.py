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


def test_tally_keeps_both_marketplaces_when_empty() -> None:
    whole, per = tally([])
    assert whole.total == 0 and set(per) == {"amazon", "flipkart"}


def _per() -> dict[str, Tally]:
    _, per = tally(
        [
            _g("amazon", "blocked", err=True),
            _g("amazon", "validated", n=2),
            _g("amazon", "approved", n=3),
            _g("flipkart", "approved"),
        ]
    )
    return per


@pytest.mark.parametrize(
    ("role", "approve", "publish", "paused"),
    [
        ("owner", 2, 1, 3),
        ("reviewer", 2, 0, 3),
        ("operator", 0, 1, 3),
        ("admin", 0, 0, 0),
        ("unknown", 0, 0, 0),
    ],
)
def test_needs_you_follows_role_and_switch(
    role: str, approve: int, publish: int, paused: int
) -> None:
    counts = needs_you_counts(
        _per(), 4, 2, Permissions.of(role), {"amazon": False, "flipkart": True}
    )
    assert counts["ready_to_approve"] == approve
    assert counts["ready_to_publish"] == publish
    assert counts["publishing_paused"] == paused
    assert counts["total"] == 1 + 4 + approve + publish
    assert counts["blocked"] == 1 and counts["questions"] == 4 and counts["improve"] == 2


def test_needs_you_counts_every_approved_listing_while_publishing_is_on() -> None:
    counts = needs_you_counts(_per(), 0, 0, Permissions.of("owner"), {"amazon": True})
    assert counts["ready_to_publish"] == 4 and counts["publishing_paused"] == 0


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
