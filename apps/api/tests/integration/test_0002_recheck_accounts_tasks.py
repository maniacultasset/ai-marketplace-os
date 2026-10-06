"""Re-check attacks on the S7 round-1 fixes (DATABASE.md §5.5 marketplace_accounts Index note,
T31; §5.7 human_tasks CHECKs): the global live seller-account key must hold on UPDATE paths too
(un-revoke, marketplace switch, seller_id rewrite), and an account task with a NULL path must not
slip past ck_human_tasks_account_target through any kind or status."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from . import factories as f
from .integrity_support import disable_0004_guard

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])


@pytest.fixture(autouse=True)
def _0004_guards_off(request: pytest.FixtureRequest) -> None:
    """These suites test 0002's CHECKs on rows the 0004 guards would refuse first (each guard is
    proven by its own test_0004_* suite); the guards are off in the rolled-back `db`
    transaction."""
    if "db" in request.fixturenames:
        disable_0004_guard(request.getfixturevalue("db"), "human_tasks", "human_tasks_guard")


Conn = psycopg.Connection[tuple[Any, ...]]

SELLER_KEY = "uq_marketplace_accounts_marketplace_seller_id"
REVOKE = (
    "UPDATE marketplace_accounts SET status = 'revoked', token_enc = NULL, "
    "kms_key_version = NULL, revoked_at = now() WHERE id = %s"
)


def test_seller_key_holds_on_unrevoke(db: Conn) -> None:
    """Brand A revokes, brand B connects the same seller account, then A's row is flipped back to
    live: the UPDATE must hit the global key, not only an INSERT."""
    first_tenant, second_tenant = f.tenant(db), f.tenant(db)
    first = f.marketplace_account(db, first_tenant, seller_id="S-UNREVOKE")
    db.execute(REVOKE, (first,))
    f.marketplace_account(db, second_tenant, seller_id="S-UNREVOKE")
    f.expect_violation(
        db,
        "23505",
        SELLER_KEY,
        lambda: db.execute(
            "UPDATE marketplace_accounts SET status = 'active', token_enc = %s, "
            "kms_key_version = '1', revoked_at = NULL WHERE id = %s",
            (b"\x02" * 48, first),
        ),
    )


def test_seller_key_holds_on_seller_id_and_marketplace_rewrite(db: Conn) -> None:
    """A live row may not be rewritten onto another tenant's live seller account, by seller_id or
    by marketplace."""
    first_tenant, second_tenant = f.tenant(db), f.tenant(db)
    f.marketplace_account(db, first_tenant, seller_id="S-TAKEN")
    f.marketplace_account(db, first_tenant, marketplace="flipkart", seller_id="S-FK")
    other = f.marketplace_account(db, second_tenant, seller_id="S-OTHER")
    f.expect_violation(
        db,
        "23505",
        SELLER_KEY,
        lambda: db.execute(
            "UPDATE marketplace_accounts SET seller_id = 'S-TAKEN' WHERE id = %s", (other,)
        ),
    )
    other_fk = f.marketplace_account(db, second_tenant, marketplace="flipkart", seller_id="S-TAKEN")
    # switching the flipkart row to amazon would collide twice; either key may fire first
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        db.execute(
            "UPDATE marketplace_accounts SET marketplace = 'amazon' WHERE id = %s", (other_fk,)
        )


def test_seller_key_allows_many_revoked_rows(db: Conn) -> None:
    """Revoked history never blocks: three brands in turn connect and revoke one seller account,
    then a fourth goes live."""
    for _ in range(3):
        db.execute(REVOKE, (f.marketplace_account(db, f.tenant(db), seller_id="S-HIST"),))
    f.marketplace_account(db, f.tenant(db), seller_id="S-HIST")
    (live,) = db.execute(
        "SELECT count(*) FROM marketplace_accounts WHERE seller_id = 'S-HIST' "
        "AND status <> 'revoked'"
    ).fetchone() or (None,)
    assert live == 1


def test_seller_key_matches_model_and_migration(db: Conn) -> None:
    """The index the migration built is the one the model declares: unique, partial on live rows,
    on (marketplace, seller_id) in that order."""
    row = db.execute(
        "SELECT ix.indisunique, pg_get_expr(ix.indpred, ix.indrelid), "
        "array(SELECT a.attname FROM unnest(ix.indkey) WITH ORDINALITY k(n, o) "
        "JOIN pg_attribute a ON a.attrelid = ix.indrelid AND a.attnum = k.n ORDER BY k.o) "
        "FROM pg_index ix JOIN pg_class c ON c.oid = ix.indexrelid WHERE c.relname = %s",
        (SELLER_KEY,),
    ).fetchone()
    assert row is not None, "index missing"
    unique, predicate, columns = row
    assert unique is True
    assert columns == ["marketplace", "seller_id"]
    assert "revoked" in predicate and "<>" in predicate


ACCOUNT_KINDS = ("reconnect_account", "permission_decision", "marketplace_error_review")


@pytest.mark.parametrize("kind", ACCOUNT_KINDS)
def test_account_task_null_path_rejected_on_update(db: Conn, kind: str) -> None:
    """The NULL-path hole must stay closed when an existing account task is UPDATEd to NULL (no
    guard trigger until 0004, so the CHECK is the only wall)."""
    tenant_id = f.tenant(db)
    account = f.marketplace_account(db, tenant_id)
    task = f.human_task(
        db,
        tenant_id,
        kind=kind,
        product_id=None,
        resume_from="publish",
        marketplace_account_id=account,
        target_path="account.category_approvals.kurtas",
    )
    f.expect_violation(
        db,
        "23514",
        "ck_human_tasks_account_target",
        lambda: db.execute("UPDATE human_tasks SET target_path = NULL WHERE id = %s", (task,)),
    )


@pytest.mark.parametrize("kind", ACCOUNT_KINDS)
def test_one_live_account_task_per_path(db: Conn, kind: str) -> None:
    """With a path now required, ht_one_live_account dedupes each account kind; a different path
    (another category) is another task, and a cancelled one frees the slot."""
    tenant_id = f.tenant(db)
    account = f.marketplace_account(db, tenant_id)
    columns: dict[str, Any] = {
        "kind": kind,
        "product_id": None,
        "resume_from": "publish",
        "marketplace_account_id": account,
        "target_path": "account.category_approvals.kurtas",
    }
    first = f.human_task(db, tenant_id, **columns)
    f.expect_violation(
        db, "23505", "ht_one_live_account", lambda: f.human_task(db, tenant_id, **columns)
    )
    f.human_task(db, tenant_id, **{**columns, "target_path": "account.category_approvals.sarees"})
    db.execute(
        "UPDATE human_tasks SET status = 'cancelled', cancelled_reason = 'superseded' "
        "WHERE id = %s",
        (first,),
    )
    f.human_task(db, tenant_id, **columns)
