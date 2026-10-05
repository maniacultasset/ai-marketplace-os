"""`human_tasks` and `listing_issues` (DATABASE.md §5.7; T20, T50, T56): one live question per
fact or account target, the task state CHECKs, one open issue per dedupe key, the resolution
CHECKs, the plain `rule_id` FK and the soft references."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.types.json import Jsonb

from . import factories as f

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
ANSWERED: dict[str, Any] = {
    "status": "answered",
    "answer": {"value": "cotton"},
    "answered_at": "2026-10-05",
}


def _answered(db: Conn, tenant_id: UUID) -> dict[str, Any]:
    return {**ANSWERED, "answered_by_user_id": f.member(db, tenant_id)}


# ---- human_tasks ----


def test_ht_one_live_fact(db: Conn) -> None:
    tenant_id = f.tenant(db)
    style = f.product(db, tenant_id, "style")
    f.human_task(db, tenant_id, product_id=style)
    f.expect_violation(
        db, "23505", "ht_one_live_fact", lambda: f.human_task(db, tenant_id, product_id=style)
    )
    f.expect_violation(
        db,
        "23505",
        "ht_one_live_fact",
        lambda: f.human_task(db, tenant_id, product_id=style, **_answered(db, tenant_id)),
    )
    # a colour-level fact is one task per colourway
    red = f.colourway(db, tenant_id, style)
    f.human_task(db, tenant_id, product_id=style, colourway_id=red)
    f.human_task(db, tenant_id, product_id=style, target_path="facts.care")
    # a brand fact is one task for the whole brand (NULLS NOT DISTINCT on product_id)
    brand = {"product_id": None, "target_path": "brand.default_manufacturer"}
    f.human_task(db, tenant_id, **brand)
    f.expect_violation(
        db, "23505", "ht_one_live_fact", lambda: f.human_task(db, tenant_id, **brand)
    )
    # a cancelled task frees its target
    db.execute(
        "UPDATE human_tasks SET status = 'cancelled', cancelled_reason = 'superseded' "
        "WHERE tenant_id = %s AND product_id IS NULL",
        (tenant_id,),
    )
    f.human_task(db, tenant_id, **brand)


def test_ht_one_live_account(db: Conn) -> None:
    tenant_id = f.tenant(db)
    account = f.marketplace_account(db, tenant_id)
    task: dict[str, Any] = {
        "kind": "permission_decision",
        "product_id": None,
        "marketplace_account_id": account,
        "target_path": "account.category_approvals.kurtas",
        "resume_from": "publish",
    }
    f.human_task(db, tenant_id, **task)
    f.expect_violation(
        db, "23505", "ht_one_live_account", lambda: f.human_task(db, tenant_id, **task)
    )
    f.human_task(db, tenant_id, **{**task, "target_path": "account.category_approvals.sarees"})
    f.human_task(db, tenant_id, **{**task, "kind": "marketplace_error_review"})


def test_human_tasks_defaults(db: Conn) -> None:
    tenant_id = f.tenant(db)
    task = f.human_task(db, tenant_id)
    row = db.execute(
        "SELECT status, suggestions, evidence FROM human_tasks WHERE id = %s", (task,)
    ).fetchone()
    assert row == ("open", [], [])


ACCOUNT_KIND = {"kind": "reconnect_account", "product_id": None, "resume_from": "publish"}
TASK_REJECTS: dict[str, tuple[dict[str, Any], str]] = {
    "kind": ({"kind": "question"}, "ck_human_tasks_kind"),
    "status": ({"status": "done"}, "ck_human_tasks_status"),
    "resume_from": ({"resume_from": "approve"}, "ck_human_tasks_resume_from"),
    "cancelled_reason": (
        {"status": "cancelled", "cancelled_reason": "bored"},
        "ck_human_tasks_cancelled_reason",
    ),
    "target_path_bad_root": ({"target_path": "listing.title"}, "ck_human_tasks_target_path"),
    "target_path_three_segments": (
        {"target_path": "facts.a.b.c"},
        "ck_human_tasks_target_path",
    ),
    "target_path_uppercase": ({"target_path": "facts.Fabric"}, "ck_human_tasks_target_path"),
    "target_path_colour_in_path": (
        {"target_path": "facts.navy blue"},
        "ck_human_tasks_target_path",
    ),
    "question 4": ({"question": "Why?"}, "ck_human_tasks_question"),
    "question 501": ({"question": "x" * 501}, "ck_human_tasks_question"),
    "reason 501": ({"reason": "x" * 501}, "ck_human_tasks_reason"),
    "expected_answer array": ({"expected_answer": Jsonb([])}, "ck_human_tasks_expected_answer"),
    "suggestions 11": (
        {"suggestions": Jsonb([{"value": str(i)} for i in range(11)])},
        "ck_human_tasks_suggestions",
    ),
    "suggestions object": ({"suggestions": {"value": "x"}}, "ck_human_tasks_suggestions"),
    "evidence object": ({"evidence": {"kind": "x"}}, "ck_human_tasks_evidence"),
    "fact without target": ({"target_path": None}, "ck_human_tasks_fact_target"),
    "brand path on a product": (
        {"target_path": "brand.default_manufacturer"},
        "ck_human_tasks_fact_target",
    ),
    "facts path without product": ({"product_id": None}, "ck_human_tasks_fact_target"),
    "fact kind on an account path": (
        {"target_path": "account.settings"},
        "ck_human_tasks_fact_target",
    ),
    "account kind without account": (
        {**ACCOUNT_KIND, "target_path": "account.token"},
        "ck_human_tasks_account_target",
    ),
    "image_replacement without image": (
        {"kind": "image_replacement", "target_path": None},
        "ck_human_tasks_image",
    ),
    "open with an answer": ({"answer": {"value": "x"}}, "ck_human_tasks_open_unanswered"),
    "answered without answerer": (ANSWERED, "ck_human_tasks_answered"),
    "applied without applied_at": (
        {**ANSWERED, "status": "applied"},
        "ck_human_tasks_answered",
    ),
    "applied_at on open": ({"applied_at": "2026-10-05"}, "ck_human_tasks_applied"),
    "cancelled without reason": ({"status": "cancelled"}, "ck_human_tasks_cancelled"),
    "reason while open": ({"cancelled_reason": "superseded"}, "ck_human_tasks_cancelled"),
}


@pytest.mark.parametrize("case", sorted(TASK_REJECTS))
def test_checks_reject_bounds_issues_tasks(db: Conn, case: str) -> None:
    columns, constraint = TASK_REJECTS[case]
    tenant_id = f.tenant(db)
    f.expect_violation(db, "23514", constraint, lambda: f.human_task(db, tenant_id, **columns))


def test_checks_reject_tasks_needing_rows(db: Conn) -> None:
    tenant_id = f.tenant(db)
    style = f.product(db, tenant_id, "style")
    account = f.marketplace_account(db, tenant_id)
    f.expect_violation(
        db,
        "23514",
        "ck_human_tasks_colourway",
        lambda: f.human_task(
            db,
            tenant_id,
            kind="image_replacement",
            product_id=None,
            target_path=None,
            image_id=f.image(db, tenant_id, style),
            colourway_id=f.colourway(db, tenant_id, style),
        ),
    )
    f.expect_violation(
        db,
        "23514",
        "ck_human_tasks_account_target",
        lambda: f.human_task(
            db, tenant_id, **ACCOUNT_KIND, marketplace_account_id=account, target_path="facts.x"
        ),
    )
    answered = _answered(db, tenant_id)
    f.expect_violation(
        db,
        "23514",
        "ck_human_tasks_applied",
        lambda: f.human_task(db, tenant_id, **{**answered, "applied_at": "2026-10-05"}),
    )


def test_checks_accept_tasks(db: Conn) -> None:
    """Every kind with its targets, and every status with its fields."""
    tenant_id = f.tenant(db)
    style = f.product(db, tenant_id, "style")
    account = f.marketplace_account(db, tenant_id)
    for kind in (
        "missing_fact",
        "conflicting_facts",
        "unverified_declaration",
        "approval_or_evidence",
    ):
        f.human_task(db, tenant_id, kind=kind, product_id=style, target_path=f"facts.{kind}")
    for kind in ("reconnect_account", "permission_decision", "marketplace_error_review"):
        f.human_task(
            db,
            tenant_id,
            **{**ACCOUNT_KIND, "kind": kind},
            marketplace_account_id=account,
            target_path="account.listing_permissions",
        )
    f.human_task(
        db,
        tenant_id,
        kind="image_replacement",
        product_id=style,
        target_path=None,
        image_id=f.image(db, tenant_id, style),
        resume_from="validate",
    )
    answered = _answered(db, tenant_id)
    f.human_task(
        db, tenant_id, target_path="facts.fabric", question="x" * 500, reason="x" * 5, **answered
    )
    f.human_task(db, tenant_id, **{**answered, "status": "applied", "applied_at": "2026-10-05"})
    f.human_task(db, tenant_id, status="cancelled", cancelled_reason="issues_closed")
    f.human_task(
        db,
        tenant_id,
        suggestions=Jsonb([{"value": str(i), "label": "x", "source": "seller"} for i in range(10)]),
        evidence=Jsonb([{"kind": "raw_marketplace_message", "text": "x"}]),
        resume_batch_id=f.batch(db, tenant_id, kind="regenerate"),
    )


# ---- listing_issues ----


def _listing(db: Conn) -> tuple[UUID, UUID]:
    tenant_id = f.tenant(db)
    return tenant_id, f.listing(db, tenant_id)


def test_li_one_open(db: Conn) -> None:
    tenant_id, listing_id = _listing(db)
    f.listing_issue(db, tenant_id, listing_id)
    f.expect_violation(
        db, "23505", "li_one_open", lambda: f.listing_issue(db, tenant_id, listing_id)
    )
    # the sync's ON CONFLICT target
    db.execute(
        "INSERT INTO listing_issues (tenant_id, listing_id, marketplace, source, field_path, "
        "error_code, error_class, retry_class, severity, message, dedupe_key) "
        "VALUES (%s, %s, 'amazon', 'rules_engine', 'content.title', 'FIELD_TOO_LONG', "
        "'content_error', 'ai_correct', 'error', 'm', 'rules_engine|content.title|FIELD_TOO_LONG|') "
        "ON CONFLICT (listing_id, dedupe_key) WHERE status = 'open' "
        "DO UPDATE SET occurrences = listing_issues.occurrences + 1, last_seen_at = now()",
        (tenant_id, listing_id),
    )
    row = db.execute(
        "SELECT occurrences FROM listing_issues WHERE listing_id = %s", (listing_id,)
    ).fetchone()
    assert row == (2,)
    db.execute(
        "UPDATE listing_issues SET status = 'resolved', resolution = 'ai_correction', "
        "resolved_at = now() WHERE listing_id = %s",
        (listing_id,),
    )
    f.listing_issue(db, tenant_id, listing_id)  # a recurrence opens a new row


def test_issue_soft_refs_have_no_fk(db: Conn) -> None:
    rows = db.execute(
        "SELECT con.conname FROM pg_constraint con WHERE con.contype = 'f' "
        "AND con.conrelid = 'listing_issues'::regclass AND EXISTS (SELECT 1 FROM pg_attribute a "
        "WHERE a.attrelid = con.conrelid AND a.attnum = ANY (con.conkey) AND a.attname IN "
        "('human_task_id', 'recurrence_of_id', 'job_id', 'validation_run_id', "
        "'last_validation_run_id', 'error_map_id'))"
    ).fetchall()
    assert rows == []
    tenant_id, listing_id = _listing(db)
    ghost = "00000000-0000-0000-0000-0000000000ff"
    f.listing_issue(
        db,
        tenant_id,
        listing_id,
        human_task_id=ghost,
        recurrence_of_id=ghost,
        job_id=ghost,
        validation_run_id=ghost,
        last_validation_run_id=ghost,
        error_map_id=ghost,
    )


def test_issue_rule_fk_plain(db: Conn) -> None:
    """The documented exception (DATABASE.md §5.2, §5.7): `rule_id → rules (id)` is a plain FK,
    since a composite FK can't reach a platform rule; the 0004 guard checks the tenant."""
    row = db.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'fk_listing_issues_rule_id_rules'"
    ).fetchone()
    assert row == ("FOREIGN KEY (rule_id) REFERENCES rules(id)",)
    tenant_id, listing_id = _listing(db)
    f.listing_issue(db, tenant_id, listing_id, rule_id=f.rule(db))
    f.listing_issue(db, tenant_id, listing_id, rule_id=f.rule(db, tenant_id))
    f.expect_violation(
        db,
        "23503",
        "fk_listing_issues_rule_id_rules",
        lambda: f.listing_issue(
            db, tenant_id, listing_id, rule_id="00000000-0000-0000-0000-0000000000fe"
        ),
    )


def test_issue_item_and_version_same_listing(db: Conn) -> None:
    tenant_id, listing_id = _listing(db)
    other = f.listing(db, tenant_id)
    other_item = f.listing_item(db, tenant_id, other, f.product(db, tenant_id))
    f.expect_violation(
        db,
        "23503",
        "fk_listing_issues_listing_item_id_listing_id",
        lambda: f.listing_issue(db, tenant_id, listing_id, listing_item_id=other_item),
    )
    f.expect_violation(
        db,
        "23503",
        "fk_listing_issues_listing_version_id",
        lambda: f.listing_issue(
            db, tenant_id, listing_id, listing_version_id=f.version(db, tenant_id, other)
        ),
    )
    f.expect_violation(
        db,
        "23503",
        "fk_listing_issues_listing_id_marketplace",
        lambda: f.listing_issue(db, tenant_id, listing_id, marketplace="flipkart"),
    )


RESOLVED: dict[str, Any] = {"status": "resolved", "resolution": "human_edit", "resolved_at": "now"}
ISSUE_REJECTS: dict[str, tuple[dict[str, Any], str]] = {
    "source ai_model": ({"source": "ai_model"}, "ck_listing_issues_source"),
    "error_class": ({"error_class": "typo"}, "ck_listing_issues_error_class"),
    "retry_class": ({"retry_class": "later"}, "ck_listing_issues_retry_class"),
    "severity": ({"severity": "info"}, "ck_listing_issues_severity"),
    "fix_action": ({"auto_fixable": True, "fix_action": "guess"}, "ck_listing_issues_fix_action"),
    "auto_fixable without action": ({"auto_fixable": True}, "li_fix"),
    "action without auto_fixable": ({"fix_action": "reformat"}, "li_fix"),
    "status": ({**RESOLVED, "status": "closed"}, "ck_listing_issues_status"),
    "resolution": ({**RESOLVED, "resolution": "magic"}, "ck_listing_issues_resolution"),
    "open with resolved_at": ({"resolved_at": "now"}, "ck_listing_issues_open"),
    "open with a resolution": ({"resolution": "human_edit"}, "ck_listing_issues_open"),
    "resolved without resolved_at": (
        {"status": "resolved", "resolution": "human_edit"},
        "ck_listing_issues_open",
    ),
    "auto_fixed by the AI": (
        {**RESOLVED, "status": "auto_fixed", "resolution": "ai_correction"},
        "ck_listing_issues_auto_fixed",
    ),
    "resolved as accepted_warning": (
        {**RESOLVED, "resolution": "accepted_warning"},
        "ck_listing_issues_resolved",
    ),
    "resolved as deterministic_fix": (
        {**RESOLVED, "resolution": "deterministic_fix"},
        "ck_listing_issues_resolved",
    ),
    "dismissed error": (
        {**RESOLVED, "status": "dismissed", "resolution": "accepted_warning"},
        "ck_listing_issues_dismissed",
    ),
    "dismissed without resolver": (
        {
            **RESOLVED,
            "status": "dismissed",
            "resolution": "accepted_warning",
            "severity": "warning",
        },
        "ck_listing_issues_dismissed",
    ),
    "last seen before first": ({"last_seen_at": "2000-01-01"}, "ck_listing_issues_seen"),
    "occurrences 0": ({"occurrences": 0}, "ck_listing_issues_occurrences"),
    "dedupe prefix": ({"dedupe_key": "rules_engine|content.title|OTHER|"}, "li_key_prefix"),
    "dedupe prefix of a lookalike path": (
        {"dedupe_key": "rules_engine|contentXtitle|FIELD_TOO_LONG|"},
        "li_key_prefix",
    ),
    "field_path 193": (
        {"field_path": "f" * 193, "dedupe_key": "rules_engine|" + "f" * 193 + "|FIELD_TOO_LONG|"},
        "ck_listing_issues_field_path",
    ),
    "raw_message_4001": ({"raw_message": "x" * 4001}, "ck_listing_issues_raw_message"),
    "marketplace_code 129": ({"marketplace_code": "x" * 129}, "ck_listing_issues_marketplace_code"),
    "message 1001": ({"message": "x" * 1001}, "ck_listing_issues_message"),
    "suggestion 1001": ({"suggestion": "x" * 1001}, "ck_listing_issues_message"),
    "value_submitted 601": ({"value_submitted": "x" * 601}, "ck_listing_issues_value_submitted"),
    "marketplace": ({"marketplace": "x"}, "ck_listing_issues_marketplace"),
}


@pytest.mark.parametrize("case", sorted(ISSUE_REJECTS))
def test_checks_reject_listing_issues(db: Conn, case: str) -> None:
    columns, constraint = ISSUE_REJECTS[case]
    tenant_id, listing_id = _listing(db)
    f.expect_violation(
        db, "23514", constraint, lambda: f.listing_issue(db, tenant_id, listing_id, **columns)
    )


def test_checks_accept_listing_issues(db: Conn) -> None:
    tenant_id, listing_id = _listing(db)
    item = f.listing_item(db, tenant_id, listing_id, f.product(db, tenant_id))
    reviewer = f.member(db, tenant_id, role="reviewer")
    f.listing_issue(
        db,
        tenant_id,
        listing_id,
        source="amazon_adapter",
        error_code="MARKETPLACE_REJECTED",
        error_class="unknown",
        retry_class="user_action",
        listing_item_id=item,
        listing_version_id=f.version(db, tenant_id, listing_id),
        marketplace_code="x" * 128,
        raw_message="x" * 4000,
        message="x" * 1000,
        plain_message="x" * 1000,
        suggestion="x" * 1000,
        value_submitted="x" * 600,
        field_path="f" * 192,
        dedupe_key="amazon_adapter|" + "f" * 192 + f"|MARKETPLACE_REJECTED|8001|item:{item}",
    )
    for status, resolution in (
        ("auto_fixed", "deterministic_fix"),
        ("resolved", "ai_correction"),
        ("resolved", "human_answer"),
        ("resolved", "regenerated"),
        ("resolved", "revalidated_clean"),
        ("resolved", "marketplace_cleared"),
    ):
        f.listing_issue(
            db,
            tenant_id,
            listing_id,
            status=status,
            resolution=resolution,
            resolved_at="now",
            auto_fixable=status == "auto_fixed",
            fix_action="reformat" if status == "auto_fixed" else None,
        )
    f.listing_issue(
        db,
        tenant_id,
        listing_id,
        severity="warning",
        status="dismissed",
        resolution="accepted_warning",
        resolved_at="now",
        resolved_by_user_id=reviewer,
    )


def test_plain_fk_exception_is_rules_only(db: Conn) -> None:
    rows = db.execute(
        "SELECT con.conname FROM pg_constraint con WHERE con.contype = 'f' "
        "AND con.connamespace = 'public'::regnamespace AND con.confrelid = 'rules'::regclass"
    ).fetchall()
    assert rows == [("fk_listing_issues_rule_id_rules",)]


# ---- cross-tenant FKs ----

Build = Callable[[Conn, UUID, UUID], None]


def _task(column: str) -> Build:
    def build(db: Conn, a: UUID, b: UUID) -> None:
        style_b = f.product(db, b, "style")
        columns: dict[str, Any]
        if column == "product_id":
            columns = {"product_id": f.product(db, b)}
        elif column == "colourway_id":
            columns = {"colourway_id": f.colourway(db, b, style_b)}
        elif column == "marketplace_account_id":
            columns = {
                **ACCOUNT_KIND,
                "marketplace_account_id": f.marketplace_account(db, b),
                "target_path": "account.token",
            }
        elif column == "image_id":
            columns = {"kind": "image_replacement", "image_id": f.image(db, b, style_b)}
        elif column == "answered_by_user_id":
            columns = {**ANSWERED, "answered_by_user_id": f.member(db, b)}
        else:
            assert column == "resume_batch_id"
            columns = {"resume_batch_id": f.batch(db, b, kind="regenerate")}
        f.human_task(db, a, **columns)

    return build


def _issue_listing(db: Conn, a: UUID, b: UUID) -> None:
    f.listing_issue(db, a, f.listing(db, b))


def _issue_resolved_by(db: Conn, a: UUID, b: UUID) -> None:
    f.listing_issue(db, a, f.listing(db, a), **RESOLVED, resolved_by_user_id=f.member(db, b))


CROSS_TENANT: dict[str, tuple[Build, str]] = {
    **{
        f"human_tasks.{column}": (_task(column), f"fk_human_tasks_{column}")
        for column in (
            "product_id",
            "colourway_id",
            "marketplace_account_id",
            "image_id",
            "answered_by_user_id",
            "resume_batch_id",
        )
    },
    "listing_issues.listing_id": (_issue_listing, "fk_listing_issues_listing_id"),
    "listing_issues.resolved_by_user_id": (
        _issue_resolved_by,
        "fk_listing_issues_resolved_by_user_id",
    ),
}
CROSS_TENANT_FK_COLUMNS = frozenset(CROSS_TENANT)


@pytest.mark.parametrize("column", sorted(CROSS_TENANT))
def test_0002_cross_tenant_fk_issues_tasks(db: Conn, column: str) -> None:
    build, constraint = CROSS_TENANT[column]
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    f.expect_violation(db, "23503", constraint, lambda: build(db, tenant_a, tenant_b))
