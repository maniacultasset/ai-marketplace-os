"""The S23 analytics world: two tenants (acme, bharat) by amazon + flipkart, fixed rows in and around
the range `FROM`..`TO`, and `EXPECTED`, the hand-computed counts per tenant and marketplace.

Rows are inserted by the admin login with user triggers off for the seed (`session_replication_role
= replica`): the 0004 guards (publish gate, `ls_guard`, review backstop) model the live write path,
which these rows skip on purpose so each edge case can be placed at an exact timestamp. Every FK
value is still a real row, and every CHECK still applies. Publish rows stand in for S21's writes
(S21 builds in parallel): an `error` row marks a retryable failure with `ErrorReport.retryable`.

Edges: a row at `TO 23:59:59.999999Z` counts, one at `TO+1 00:00:00Z` does not; likewise
`FROM 00:00:00Z` counts and `FROM-1 23:59:59.999999Z` does not.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import psycopg

from . import factories as f
from .ops_support import category_template, digest, job_run

Conn = psycopg.Connection[tuple[Any, ...]]

FROM = date(2026, 9, 1)
TO = date(2026, 9, 30)
FIRST = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)
BEFORE = datetime(2026, 8, 31, 23, 59, 59, 999999, tzinfo=UTC)
LAST = datetime(2026, 9, 30, 23, 59, 59, 999999, tzinfo=UTC)
AFTER = datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC)
# the versions a decision or a submission needs sit outside every range the tests read
SETUP = datetime(2026, 7, 1, tzinfo=UTC)
_n = itertools.count(1)


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=UTC)


COUNT_KEYS = (
    "listings_generated",
    "approvals",
    "rejections",
    "validation_first_pass_passed",
    "validation_first_pass_total",
    "publish_succeeded",
    "publish_failed",
)


def counts(*values: int) -> dict[str, int]:
    return dict(zip(COUNT_KEYS, values, strict=True))


# tenant → marketplace → counts (in COUNT_KEYS order)
EXPECTED: dict[str, dict[str, dict[str, int]]] = {
    "acme": {
        "amazon": counts(4, 2, 1, 2, 3, 4, 2),
        "flipkart": counts(1, 0, 1, 0, 1, 1, 0),
    },
    "bharat": {
        "amazon": counts(3, 1, 0, 1, 1, 0, 1),
    },
}


@dataclass
class Side:
    tenant_id: UUID
    owner: UUID
    reviewer: UUID
    accounts: dict[str, UUID] = field(default_factory=dict)


def _side(conn: Conn, tenant_id: UUID) -> Side:
    owner = f.member(conn, tenant_id)
    side = Side(tenant_id, owner, f.member(conn, tenant_id, role="reviewer"))
    for marketplace in ("amazon", "flipkart"):
        side.accounts[marketplace] = f.marketplace_account(
            conn, tenant_id, marketplace=marketplace, connected_by_user_id=owner
        )
    return side


_TEMPLATE: dict[int, UUID] = {}


def _template(conn: Conn) -> UUID:
    """One Amazon template per connection (a platform row, shared by both tenants)."""
    key = id(conn)
    if key not in _TEMPLATE:
        _TEMPLATE[key] = category_template(conn)
    return _TEMPLATE[key]


def listing(conn: Conn, side: Side, marketplace: str = "amazon") -> UUID:
    product = f.product(conn, side.tenant_id)
    return f.listing(
        conn,
        side.tenant_id,
        product,
        marketplace=marketplace,
        marketplace_account_id=side.accounts[marketplace],
    )


def version(conn: Conn, side: Side, listing_id: UUID, created_at: datetime, **extra: Any) -> UUID:
    return f.version(
        conn,
        side.tenant_id,
        listing_id,
        generated_by_user_id=side.owner,
        created_at=created_at,
        **extra,
    )


def decision(conn: Conn, side: Side, listing_id: UUID, kind: str, decided_at: datetime) -> UUID:
    version_id = version(conn, side, listing_id, SETUP)
    extra: dict[str, Any] = {}
    if kind == "reject":
        extra = {"reason_code": "poor_quality", "feedback": "tone"}
    if kind == "regenerate_requested":
        extra = {"regeneration_job_id": job_run(conn, side.tenant_id, queue="default")}
    return f.review_decision(
        conn,
        side.tenant_id,
        listing_id,
        version_id,
        decision=kind,
        decided_by_user_id=side.reviewer,
        decided_at=decided_at,
        **extra,
    )


def validation(
    conn: Conn,
    side: Side,
    marketplace: str,
    passed: bool,
    created_at: datetime,
    *,
    source: str = "rules_engine",
    attempt: int = 1,
) -> UUID:
    listing_id = listing(conn, side, marketplace)
    pins: dict[str, Any] = {}
    if source == "rules_engine":
        pins = {
            "rules_version": 1,
            "category_template_id": _template(conn),
            "template_version": 1,
            "facts_digest": digest(f"an-vr-{next(_n)}"),
        }
    return f.validation_run(
        conn,
        side.tenant_id,
        listing_id,
        marketplace=marketplace,
        source=source,
        passed=passed,
        attempt=attempt,
        created_at=created_at,
        blocking_count=0 if passed else 1,
        **pins,
    )


def _report(retryable: bool) -> dict[str, Any]:
    return {"status": "failed", "error_code": "RATE_LIMITED", "retryable": retryable, "errors": []}


def submission(
    conn: Conn,
    side: Side,
    listing_id: UUID,
    version_id: UUID,
    outcome: str,
    responded_at: datetime | None,
    *,
    marketplace: str = "amazon",
    attempt: int = 1,
    previous: UUID | None = None,
    retryable: bool = False,
) -> UUID:
    n = next(_n)
    columns: dict[str, Any] = {
        "tenant_id": side.tenant_id,
        "listing_id": listing_id,
        "listing_version_id": version_id,
        "marketplace": marketplace,
        "marketplace_account_id": side.accounts[marketplace],
        "job_id": job_run(conn, side.tenant_id, created_by=side.owner, queue="default"),
        "attempt": attempt,
        "operation": "patch" if attempt > 1 else "create",
        "payload_sha256": digest(f"an-payload-{n}"),
        "payload_storage_key": f"{side.tenant_id}/submissions/an-{n}.json",
        "facts_digest": digest(f"an-facts-{n}"),
        "idempotency_key": digest(f"an-idem-{n}").hex(),
        "requested_by_user_id": side.owner,
        "actor_type": "user",
        "outcome": outcome,
        "responded_at": responded_at,
    }
    if attempt > 1:
        columns.update(
            previous_submission_id=previous,
            retry_justification="retryable_condition",
            field_paths=["content.title"],
        )
    if outcome in ("partially_accepted", "rejected", "error", "live", "superseded"):
        columns["resolved_at"] = responded_at
    if outcome in ("partially_accepted", "rejected", "error"):
        columns["error_report_json"] = _report(retryable)
    if outcome == "error":
        columns["error_code"] = "RATE_LIMITED" if retryable else "MARKETPLACE_REJECTED"
    return f.insert(conn, "listing_submissions", **columns)  # type: ignore[no-any-return]


def _acme(conn: Conn, side: Side) -> None:
    amazon = listing(conn, side)
    flip = listing(conn, side, "flipkart")
    # volume: amazon 4 (two edges in, regeneration counts), flipkart 1
    for created in (FIRST, at(15), LAST):
        version(conn, side, amazon, created)
    version(conn, side, amazon, at(20), origin="regeneration", changed_paths=["content.title"])
    version(conn, side, amazon, AFTER)
    version(conn, side, amazon, BEFORE)
    version(conn, side, amazon, at(10), origin="edit", changed_paths=["content.title"])
    version(conn, side, amazon, at(11), origin="import")
    version(conn, side, flip, at(5))
    # decisions: amazon 2 approve + 1 reject, flipkart 1 reject; out: regenerate, after the range
    decision(conn, side, amazon, "approve", at(2))
    decision(conn, side, amazon, "approve", LAST)
    decision(conn, side, amazon, "reject", at(3))
    decision(conn, side, amazon, "regenerate_requested", at(4))
    decision(conn, side, amazon, "approve", AFTER)
    decision(conn, side, flip, "reject", at(6))
    # validation: amazon 2 of 3 first-pass Rules Engine runs, flipkart 0 of 1
    validation(conn, side, "amazon", True, FIRST)
    validation(conn, side, "amazon", True, LAST)
    validation(conn, side, "amazon", False, at(7))
    validation(conn, side, "amazon", True, at(8), attempt=2)
    validation(conn, side, "amazon", False, at(8), source="schema")
    validation(conn, side, "amazon", True, AFTER)
    validation(conn, side, "amazon", True, BEFORE)
    validation(conn, side, "flipkart", False, at(9))
    # publish (amazon succeeded 4, failed 2; flipkart 1/0)
    chain_a = listing(conn, side)
    va = version(conn, side, chain_a, SETUP)
    first = submission(conn, side, chain_a, va, "error", at(10), retryable=True)  # out
    submission(conn, side, chain_a, va, "accepted", at(11), attempt=2, previous=first)
    chain_b = listing(conn, side)
    vb = version(conn, side, chain_b, SETUP)
    partial = submission(conn, side, chain_b, vb, "partially_accepted", at(12))
    submission(conn, side, chain_b, vb, "accepted", at(13), attempt=2, previous=partial)
    for outcome, when in (
        ("error", at(14)),  # non-retryable: failed
        ("pending", None),  # out
        ("superseded", at(15)),
        ("live", LAST),
        ("rejected", AFTER),  # out
        ("rejected", BEFORE),  # out
    ):
        other = listing(conn, side)
        submission(conn, side, other, version(conn, side, other, SETUP), outcome, when)
    fk = listing(conn, side, "flipkart")
    submission(
        conn, side, fk, version(conn, side, fk, SETUP), "accepted", FIRST, marketplace="flipkart"
    )


def _bharat(conn: Conn, side: Side) -> None:
    amazon = listing(conn, side)
    for day in (2, 3, 4):
        version(conn, side, amazon, at(day))
    decision(conn, side, amazon, "approve", at(5))
    validation(conn, side, "amazon", True, at(6))
    other = listing(conn, side)
    submission(conn, side, other, version(conn, side, other, SETUP), "rejected", at(7))


@dataclass
class AnalyticsWorld:
    conn: Conn
    tenants: dict[str, UUID]


def build_analytics_world(conn: Conn, tenants: dict[str, UUID] | None = None) -> AnalyticsWorld:
    """Seed both tenants (created here unless `tenants` maps `acme`/`bharat` to existing ids).
    `conn` is an admin (superuser) connection."""
    ids = dict(tenants) if tenants else {"acme": f.tenant(conn), "bharat": f.tenant(conn)}
    conn.execute("SET session_replication_role = replica")
    try:
        _acme(conn, _side(conn, ids["acme"]))
        _bharat(conn, _side(conn, ids["bharat"]))
    finally:
        conn.execute("RESET session_replication_role")
    return AnalyticsWorld(conn, ids)


def totals(slug: str) -> dict[str, int]:
    out = dict.fromkeys(COUNT_KEYS, 0)
    for per in EXPECTED[slug].values():
        for key, value in per.items():
            out[key] += value
    return out
