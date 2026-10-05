"""`listing_submissions` and `publish_field_results` (DATABASE.md §5.3; inv. 9, 10, T19): one open
submission per listing, attempts 1..3 with the anti-loop pair, the outcome CHECKs, the version and
marketplace pins, the per-field ledger key, and a `field` CHECK that agrees with
`mkos_schemas.paths.is_valid_field_path` on every input (whitespace included)."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import psycopg
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mkos_schemas.paths import FIELD_PATH_PATTERNS, MAX_PATH_LENGTH, is_valid_field_path

from . import factories as f
from .ops_support import (
    Seed,
    TenantSeed,
    field_result,
    job_run,
    seed_graph,
    submission,
)

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

# The code points Python's `\s` matches (str patterns): the class the DB CHECK spells out.
PY_WHITESPACE = tuple(chr(c) for c in range(0x110000) if re.match(r"\s", chr(c)))
OUTCOMES = ["pending", "accepted", "partially_accepted", "rejected", "error", "live", "superseded"]
TERMINAL = ["partially_accepted", "rejected", "error", "live", "superseded"]
REPORT = {"errors": [{"error_code": "VALUE_NOT_ALLOWED"}]}
NOW = "2026-10-05T00:00:00Z"


@pytest.fixture
def seed(db: Conn) -> Seed:
    return seed_graph(db)


def _refused(db: Conn, code: str, constraint: str, build: Any) -> None:
    try:
        with db.transaction():
            build()
    except psycopg.Error as error:
        assert error.sqlstate == code, error
        assert error.diag.constraint_name == constraint, error.diag.constraint_name
    else:
        raise AssertionError(f"wanted {code} on {constraint}, the insert succeeded")


def _outcome(outcome: str) -> dict[str, Any]:
    """The columns a row with `outcome` needs to pass the other CHECKs."""
    columns: dict[str, Any] = {"outcome": outcome}
    if outcome != "pending":
        columns["responded_at"] = NOW
    if outcome in TERMINAL:
        columns["resolved_at"] = NOW
    if outcome in ("partially_accepted", "rejected", "error"):
        columns["error_report_json"] = REPORT
    if outcome == "error":
        columns["error_code"] = "SUBMISSION_NOT_APPLIED"
    return columns


def _retry(db: Conn, side: TenantSeed, previous: UUID, attempt: int, **columns: Any) -> UUID:
    return submission(
        db,
        side,
        attempt=attempt,
        previous_submission_id=previous,
        retry_justification="changed_input",
        **columns,
    )


# ---- listing_submissions ----


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_listing_submissions_outcomes(db: Conn, seed: Seed, outcome: str) -> None:
    submission(db, seed.a, **_outcome(outcome))


def test_listing_submissions_outcome_default_and_vocab(db: Conn, seed: Seed) -> None:
    row_id = submission(db, seed.a)
    assert db.execute(
        "SELECT outcome FROM listing_submissions WHERE id = %s", (row_id,)
    ).fetchone() == ("pending",)
    other = seed_graph(db).a
    _refused(
        db, "23514", "ck_listing_submissions_outcome", lambda: submission(db, other, outcome="ok")
    )


def test_listing_submissions_ux_ls_open(db: Conn, seed: Seed) -> None:
    first = submission(db, seed.a)
    version_2 = f.version(db, seed.a.tenant_id, seed.a.listing_id)
    for outcome in ("pending", "accepted"):
        _refused(
            db,
            "23505",
            "ux_ls_open",
            lambda o=outcome: submission(db, seed.a, listing_version_id=version_2, **_outcome(o)),
        )
    db.execute(
        "UPDATE listing_submissions SET outcome = 'rejected', responded_at = now(), "
        "resolved_at = now(), error_report_json = '{}' WHERE id = %s",
        (first,),
    )
    _retry(db, seed.a, first, 2)
    # another listing is not blocked
    submission(db, seed.b)


@pytest.mark.parametrize("attempt", [0, 4, -1])
def test_listing_submissions_attempt_bounds(db: Conn, seed: Seed, attempt: int) -> None:
    previous = submission(db, seed.a, **_outcome("rejected"))
    _refused(
        db,
        "23514",
        "ck_listing_submissions_attempt",
        lambda: _retry(db, seed.a, previous, attempt),
    )


def test_listing_submissions_attempt_unique_per_version(db: Conn, seed: Seed) -> None:
    previous = submission(db, seed.a, **_outcome("rejected"))
    _retry(db, seed.a, previous, 2, **_outcome("error"))
    _refused(
        db,
        "23505",
        "uq_listing_submissions_listing_id_listing_version_id_attempt",
        lambda: _retry(db, seed.a, previous, 2),
    )


@pytest.mark.parametrize(
    ("operation", "paths"),
    [("patch", None), ("patch", []), ("create", ["content.title"]), ("update", [])],
)
def test_listing_submissions_patch_needs_paths(
    db: Conn, seed: Seed, operation: str, paths: list[str] | None
) -> None:
    _refused(
        db,
        "23514",
        "ck_listing_submissions_field_paths",
        lambda: submission(db, seed.a, operation=operation, field_paths=paths),
    )


def test_listing_submissions_patch_with_paths_inserts(db: Conn, seed: Seed) -> None:
    previous = submission(db, seed.a, **_outcome("partially_accepted"))
    _retry(db, seed.a, previous, 2, operation="patch", field_paths=["content.title"])
    _refused(
        db,
        "23514",
        "ck_listing_submissions_operation",
        lambda: submission(db, seed.b, operation="delete"),
    )


def test_listing_submissions_attempt1_no_previous(db: Conn, seed: Seed) -> None:
    previous = submission(db, seed.b, **_outcome("rejected"))
    for columns in (
        {"retry_justification": "changed_input"},
        {"previous_submission_id": previous},
    ):
        _refused(
            db,
            "23514",
            "ck_listing_submissions_retry_pair",
            lambda c=columns: submission(db, seed.a, **c),
        )


def test_listing_submissions_attempt2_needs_previous_and_justification(
    db: Conn, seed: Seed
) -> None:
    previous = submission(db, seed.a, **_outcome("rejected"))
    for columns in (
        {"previous_submission_id": previous},
        {"retry_justification": "changed_input"},
        {},
    ):
        _refused(
            db,
            "23514",
            "ck_listing_submissions_retry_pair",
            lambda c=columns: submission(db, seed.a, attempt=2, **c),
        )
    _refused(
        db,
        "23514",
        "ck_listing_submissions_retry_justification",
        lambda: submission(
            db, seed.a, attempt=2, previous_submission_id=previous, retry_justification="again"
        ),
    )


def test_listing_submissions_previous_same_version(db: Conn, seed: Seed) -> None:
    previous = submission(db, seed.a, **_outcome("rejected"))
    version_2 = f.version(db, seed.a.tenant_id, seed.a.listing_id)
    _refused(
        db,
        "23503",
        "fk_listing_submissions_previous_submission_id",
        lambda: _retry(db, seed.a, previous, 2, listing_version_id=version_2),
    )


def test_listing_submissions_system_needs_changed_input(db: Conn, seed: Seed) -> None:
    previous = submission(db, seed.a, **_outcome("error"))
    _refused(
        db,
        "23514",
        "ck_listing_submissions_system_changed_input",
        lambda: submission(
            db,
            seed.a,
            attempt=2,
            previous_submission_id=previous,
            retry_justification="retryable_condition",
            actor_type="system",
        ),
    )
    _refused(
        db,
        "23514",
        "ck_listing_submissions_actor_type",
        lambda: submission(db, seed.b, actor_type="agent"),
    )
    _retry(db, seed.a, previous, 2, actor_type="system")


@pytest.mark.parametrize("outcome", TERMINAL)
def test_listing_submissions_terminal_needs_resolved(db: Conn, seed: Seed, outcome: str) -> None:
    columns = {**_outcome(outcome), "resolved_at": None}
    _refused(
        db, "23514", "ck_listing_submissions_resolved", lambda: submission(db, seed.a, **columns)
    )


@pytest.mark.parametrize("outcome", ["pending", "accepted"])
def test_listing_submissions_open_not_resolved(db: Conn, seed: Seed, outcome: str) -> None:
    columns = {**_outcome(outcome), "resolved_at": NOW}
    _refused(
        db, "23514", "ck_listing_submissions_resolved", lambda: submission(db, seed.a, **columns)
    )


def test_listing_submissions_non_pending_needs_responded(db: Conn, seed: Seed) -> None:
    _refused(
        db,
        "23514",
        "ck_listing_submissions_responded",
        lambda: submission(db, seed.a, **{**_outcome("accepted"), "responded_at": None}),
    )


@pytest.mark.parametrize("outcome", ["partially_accepted", "rejected", "error"])
def test_listing_submissions_error_needs_code_and_report(
    db: Conn, seed: Seed, outcome: str
) -> None:
    _refused(
        db,
        "23514",
        "ck_listing_submissions_error_report",
        lambda: submission(db, seed.a, **{**_outcome(outcome), "error_report_json": None}),
    )
    if outcome == "error":
        _refused(
            db,
            "23514",
            "ck_listing_submissions_error_code",
            lambda: submission(db, seed.a, **{**_outcome(outcome), "error_code": None}),
        )


@pytest.mark.parametrize("column", ["payload_sha256", "facts_digest", "raw_response_sha256"])
@pytest.mark.parametrize("size", [0, 31, 33])
def test_listing_submissions_digests(db: Conn, seed: Seed, column: str, size: int) -> None:
    _refused(
        db,
        "23514",
        f"ck_listing_submissions_{column}",
        lambda: submission(db, seed.a, **{column: b"\x03" * size}),
    )


@pytest.mark.parametrize("column", ["payload_storage_key", "raw_response_storage_key"])
@pytest.mark.parametrize(
    "value", ["https://bucket.test/x.json", "/abs/x.json", "a/../b.json", "a//b.json", "gs://b/x"]
)
def test_listing_submissions_storage_key_shape(
    db: Conn, seed: Seed, column: str, value: str
) -> None:
    _refused(
        db,
        "23514",
        f"ck_listing_submissions_{column}",
        lambda: submission(db, seed.a, **{column: value}),
    )


def test_listing_submissions_idempotency_key_unique(db: Conn, seed: Seed) -> None:
    submission(db, seed.a, idempotency_key="abc")
    other = f.listing(
        db,
        seed.a.tenant_id,
        f.product(db, seed.a.tenant_id),
        marketplace_account_id=seed.a.account_id,
    )
    version = f.version(db, seed.a.tenant_id, other, generated_by_user_id=seed.a.user_id)
    _refused(
        db,
        "23505",
        "uq_listing_submissions_tenant_id_marketplace_idempotency_key",
        lambda: submission(
            db, seed.a, listing_id=other, listing_version_id=version, idempotency_key="abc"
        ),
    )
    submission(db, seed.b, idempotency_key="abc")


def test_listing_submissions_marketplace_must_match_listing(db: Conn, seed: Seed) -> None:
    flipkart = f.marketplace_account(
        db, seed.a.tenant_id, marketplace="flipkart", connected_by_user_id=seed.a.user_id
    )
    _refused(
        db,
        "23503",
        "fk_listing_submissions_listing_id_marketplace",
        lambda: submission(db, seed.a, marketplace="flipkart", marketplace_account_id=flipkart),
    )


def test_listing_submissions_account_marketplace_must_match(db: Conn, seed: Seed) -> None:
    flipkart = f.marketplace_account(
        db, seed.a.tenant_id, marketplace="flipkart", connected_by_user_id=seed.a.user_id
    )
    _refused(
        db,
        "23503",
        "fk_listing_submissions_marketplace_account_id_marketplace",
        lambda: submission(db, seed.a, marketplace_account_id=flipkart),
    )


def test_ops_cross_tenant_fk_listing_submissions(db: Conn, seed: Seed) -> None:
    # tenant B's row naming tenant A's listing (and A's version, so only the tenant pin is wrong)
    _refused(
        db,
        "23503",
        "fk_listing_submissions_listing_id",
        lambda: submission(
            db, seed.b, listing_id=seed.a.listing_id, listing_version_id=seed.a.version_id
        ),
    )


@pytest.mark.parametrize(
    ("column", "constraint"),
    [
        ("marketplace_account_id", "fk_listing_submissions_marketplace_account_id"),
        ("requested_by_user_id", "fk_listing_submissions_requested_by_user_id"),
    ],
)
def test_ops_cross_tenant_fk_listing_submissions_columns(
    db: Conn, seed: Seed, column: str, constraint: str
) -> None:
    value = seed.b.account_id if column == "marketplace_account_id" else seed.b.user_id
    _refused(db, "23503", constraint, lambda: submission(db, seed.a, **{column: value}))


def test_ops_cross_tenant_fk_listing_submissions_job(db: Conn, seed: Seed) -> None:
    other_job = job_run(db, seed.b.tenant_id)
    _refused(
        db,
        "23503",
        "fk_listing_submissions_job_id",
        lambda: submission(db, seed.a, job_id=other_job),
    )


def test_listing_submissions_version_of_other_listing_refused(db: Conn, seed: Seed) -> None:
    other = f.listing(db, seed.a.tenant_id)
    other_version = f.version(db, seed.a.tenant_id, other)
    _refused(
        db,
        "23503",
        "fk_listing_submissions_listing_version_id",
        lambda: submission(db, seed.a, listing_version_id=other_version),
    )


def test_listing_submissions_indexes(db: Conn) -> None:
    rows: dict[str, str] = dict(
        db.execute(
            "SELECT indexrelid::regclass::text, pg_get_indexdef(indexrelid) FROM pg_index "
            "WHERE indrelid = 'listing_submissions'::regclass"
        ).fetchall()
    )
    expected = {
        "ux_ls_open": "UNIQUE INDEX ux_ls_open ON public.listing_submissions USING btree "
        "(listing_id) WHERE (outcome = ANY (ARRAY['pending'::text, 'accepted'::text]))",
        "ix_listing_submissions_attempts": "INDEX ix_listing_submissions_attempts ON "
        "public.listing_submissions USING btree (tenant_id, listing_id, created_at DESC, id DESC)",
        "ix_listing_submissions_job_id": "INDEX ix_listing_submissions_job_id ON "
        "public.listing_submissions USING btree (job_id)",
        "ix_listing_submissions_submission_ref": "INDEX ix_listing_submissions_submission_ref ON "
        "public.listing_submissions USING btree (tenant_id, marketplace, submission_ref) "
        "WHERE (submission_ref IS NOT NULL)",
    }
    for name, definition in expected.items():
        assert rows[name] == f"CREATE {definition}", name


# ---- publish_field_results ----


def test_listing_submissions_pfr_unique(db: Conn, seed: Seed) -> None:
    sub = submission(db, seed.a)
    field_result(db, seed.a, sub)
    _refused(
        db,
        "23505",
        "uq_publish_field_results_submission_id_listing_item_id_field",
        lambda: field_result(db, seed.a, sub),
    )
    field_result(db, seed.a, sub, field="content.description")


def test_ops_cross_tenant_fk_publish_field_results(db: Conn, seed: Seed) -> None:
    sub = submission(db, seed.a)
    _refused(
        db,
        "23503",
        "fk_publish_field_results_submission_id",
        lambda: field_result(
            db,
            seed.b,
            sub,
            listing_id=seed.a.listing_id,
            listing_version_id=seed.a.version_id,
            listing_item_id=seed.a.item_id,
        ),
    )


def test_listing_submissions_pfr_pins_submission_version(db: Conn, seed: Seed) -> None:
    sub = submission(db, seed.a)
    version_2 = f.version(db, seed.a.tenant_id, seed.a.listing_id)
    _refused(
        db,
        "23503",
        "fk_publish_field_results_submission_id_listing_id",
        lambda: field_result(db, seed.a, sub, listing_version_id=version_2),
    )


def test_listing_submissions_pfr_item_of_listing(db: Conn, seed: Seed) -> None:
    sub = submission(db, seed.a)
    other = f.listing(db, seed.a.tenant_id)
    other_item = f.listing_item(db, seed.a.tenant_id, other, f.product(db, seed.a.tenant_id))
    _refused(
        db,
        "23503",
        "fk_publish_field_results_listing_item_id",
        lambda: field_result(db, seed.a, sub, listing_item_id=other_item),
    )


def test_listing_submissions_pfr_failed_needs_error_json(db: Conn, seed: Seed) -> None:
    sub = submission(db, seed.a)
    _refused(
        db,
        "23514",
        "ck_publish_field_results_error_json",
        lambda: field_result(db, seed.a, sub, status="failed"),
    )
    _refused(
        db,
        "23514",
        "ck_publish_field_results_error_json",
        lambda: field_result(db, seed.a, sub, status="succeeded", error_json=REPORT),
    )
    _refused(
        db,
        "23514",
        "ck_publish_field_results_status",
        lambda: field_result(db, seed.a, sub, status="submitted"),
    )
    field_result(db, seed.a, sub, status="failed", error_json=REPORT, field="content.bullets[0]")
    field_result(db, seed.a, sub, status="skipped", field="images[1]")


def test_listing_submissions_pfr_retry_index(db: Conn) -> None:
    row = db.execute(
        "SELECT pg_get_indexdef('ix_publish_field_results_retry'::regclass)"
    ).fetchone()
    assert row is not None
    assert row[0].endswith(
        "(tenant_id, listing_id, listing_version_id, listing_item_id, field, created_at DESC)"
    ), row[0]


def _db_field_pattern(db: Conn) -> str:
    row = db.execute(
        "SELECT substring(pg_get_constraintdef(oid) FROM '~ ''(.*)''::text') FROM pg_constraint "
        "WHERE conname = 'ck_publish_field_results_field'"
    ).fetchone()
    assert row is not None and row[0], row
    return str(row[0])


def _db_verdict(db: Conn, candidate: str) -> bool:
    row = db.execute(
        "SELECT %s ~ %s AND char_length(%s) <= %s",
        (candidate, _db_field_pattern(db), candidate, MAX_PATH_LENGTH),
    ).fetchone()
    assert row is not None
    return bool(row[0])


def test_listing_submissions_pfr_field_pattern_is_frozen_paths(db: Conn) -> None:
    """The CHECK is FIELD_PATH_PATTERNS joined by `|`, with Python's `\\s` spelled out as the
    29 code points it matches: a pattern added to paths.py alone goes red here."""
    whitespace = "".join(f"\\u{ord(c):04x}" for c in PY_WHITESPACE)
    expected = "|".join(
        p.replace(r"[^\]\s]", r"[^\]" + whitespace + "]") for p in FIELD_PATH_PATTERNS
    )
    assert len(PY_WHITESPACE) == 29
    assert _db_field_pattern(db) == expected


@pytest.mark.parametrize(
    ("candidate", "valid"),
    [
        ("variants[SKU-1].facts.color", True),
        ("variants[SKU-ü-1]", True),
        ("content.bullets[12]", True),
        ("variants[a\x1cb]", False),
        ("variants[a\xa0b]", False),
        ("variants[a　b]", False),
        ("variants[a]b]", False),
        ("content.title\n", False),
        ("Content.title", False),
        ("attributes." + "a" * 64, True),
        ("attributes." + "a" * 65, False),
    ],
)
def test_listing_submissions_pfr_field_examples(db: Conn, candidate: str, valid: bool) -> None:
    assert is_valid_field_path(candidate) is valid
    assert _db_verdict(db, candidate) is valid


def test_listing_submissions_pfr_field_length_193_refused(db: Conn, seed: Seed) -> None:
    sub = submission(db, seed.a)
    long_path = "variants[" + "s" * 64 + "].facts." + "a" * 63
    assert len(long_path) <= MAX_PATH_LENGTH and is_valid_field_path(long_path)
    field_result(db, seed.a, sub, field=long_path)
    too_long = "x" * (MAX_PATH_LENGTH + 1)
    assert not is_valid_field_path(too_long)
    _refused(
        db,
        "23514",
        "ck_publish_field_results_field",
        lambda: field_result(db, seed.a, sub, field=too_long),
    )


_SAFE_TEXT = st.characters(codec="utf-8", exclude_characters="\x00")


@st.composite
def _injected(draw: st.DrawFn) -> str:
    """A valid `variants[…]` path with one whitespace code point (or `]`) inside the bracket."""
    sku = draw(
        st.text(
            alphabet=st.characters(codec="utf-8", exclude_characters="\x00]"),
            min_size=1,
            max_size=30,
        )
    )
    bad = draw(st.sampled_from((*PY_WHITESPACE, "]")))
    cut = draw(st.integers(min_value=0, max_value=len(sku)))
    return f"variants[{sku[:cut]}{bad}{sku[cut:]}]"


@settings(
    max_examples=400, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    candidate=st.one_of(
        *(st.from_regex(p, fullmatch=True, alphabet=_SAFE_TEXT) for p in FIELD_PATH_PATTERNS),
        _injected(),
        st.text(alphabet=_SAFE_TEXT, max_size=200),
    )
)
def test_listing_submissions_pfr_field_check_matches_paths(db: Conn, candidate: str) -> None:
    assert _db_verdict(db, candidate) == is_valid_field_path(candidate), repr(candidate)
