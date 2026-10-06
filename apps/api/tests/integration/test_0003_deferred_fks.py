"""The three FKs 0003 adds onto 0002 tables with `NOT VALID` then `VALIDATE` (DATABASE.md §8.2
ledger row 0003; §5.3 listing_versions and validation_runs, §5.7 listing_issues): each exists and is
validated, and each refuses a row that names no template or error-map row."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import disable_0004_guard
from .ops_support import category_template, error_map

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])


@pytest.fixture(autouse=True)
def _0004_guards_off(request: pytest.FixtureRequest) -> None:
    """These suites test 0003's CHECKs, keys and RESTRICT FKs on platform rows the 0004 platform
    guards would refuse first (test_0004_platform_guards.py proves them); the guards are off in
    the rolled-back `db` transaction."""
    if "db" in request.fixturenames:
        db = request.getfixturevalue("db")
        disable_0004_guard(db, "category_templates", "category_templates_guard")
        disable_0004_guard(db, "marketplace_error_map", "error_map_guard")
        disable_0004_guard(db, "attribute_definitions", "dictionary_guard")
        disable_0004_guard(db, "category_attributes", "dictionary_guard")


Conn = psycopg.Connection[tuple[Any, ...]]

DEFERRED = {
    "li_error_map_fk": (
        "listing_issues",
        "FOREIGN KEY (error_map_id) REFERENCES marketplace_error_map(id)",
    ),
    "fk_validation_runs_category_template_id_category_templates": (
        "validation_runs",
        "FOREIGN KEY (category_template_id, template_version) "
        "REFERENCES category_templates(id, version)",
    ),
    "fk_listing_versions_category_template_id_category_templates": (
        "listing_versions",
        "FOREIGN KEY (category_template_id) REFERENCES category_templates(id)",
    ),
}
DIGEST = b"\x05" * 32


def test_deferred_fks_validated(db: Conn) -> None:
    rows = db.execute(
        "SELECT conname, conrelid::regclass::text, pg_get_constraintdef(oid), convalidated "
        "FROM pg_constraint WHERE conname = ANY(%s) ORDER BY 1",
        (list(DEFERRED),),
    ).fetchall()
    assert {r[0]: (r[1], r[2]) for r in rows} == DEFERRED
    assert all(r[3] for r in rows), rows


def test_deferred_fks_refuse_unknown_template(db: Conn) -> None:
    tenant_id = f.tenant(db)
    listing_id = f.listing(db, tenant_id)
    template = category_template(db, version=1)
    pins = {"rules_version": 7, "facts_digest": DIGEST, "source": "rules_engine"}
    for template_id, version in ((uuid4(), 1), (template, 2)):
        try:
            with db.transaction():
                f.validation_run(
                    db,
                    tenant_id,
                    listing_id,
                    category_template_id=template_id,
                    template_version=version,
                    **pins,
                )
        except psycopg.Error as error:
            assert error.sqlstate == "23503"
            assert (
                error.diag.constraint_name
                == "fk_validation_runs_category_template_id_category_templates"
            )
        else:
            raise AssertionError(f"a pin to ({template_id}, {version}) inserted")
    f.validation_run(
        db, tenant_id, listing_id, category_template_id=template, template_version=1, **pins
    )
    # MATCH SIMPLE: no pin, no check
    f.validation_run(db, tenant_id, listing_id)


def test_deferred_fks_refuse_unknown_version_template(db: Conn) -> None:
    tenant_id = f.tenant(db)
    listing_id = f.listing(db, tenant_id)
    expect_sqlstate(
        db,
        "23503",
        "INSERT INTO listing_versions (tenant_id, listing_id, version, content_json, origin, "
        "generated_by_user_id, category_template_id) VALUES (%s, %s, 1, '{}', 'generation', %s, %s)",
        (tenant_id, listing_id, f.member(db, tenant_id), uuid4()),
        constraint="fk_listing_versions_category_template_id_category_templates",
    )
    f.version(db, tenant_id, listing_id, category_template_id=category_template(db))


def test_deferred_fks_refuse_unknown_error_map(db: Conn) -> None:
    tenant_id = f.tenant(db)
    listing_id = f.listing(db, tenant_id)
    try:
        with db.transaction():
            f.listing_issue(db, tenant_id, listing_id, error_map_id=uuid4())
    except psycopg.Error as error:
        assert error.sqlstate == "23503"
        assert error.diag.constraint_name == "li_error_map_fk"
    else:
        raise AssertionError("an issue naming no error-map row inserted")
    f.listing_issue(db, tenant_id, listing_id, error_map_id=error_map(db))


def test_deferred_fks_restrict_template_delete(db: Conn) -> None:
    """Validation runs and versions pin templates forever (DATABASE.md §5.2 guard note): the
    plain NO ACTION FK refuses a delete of a pinned template."""
    tenant_id = f.tenant(db)
    listing_id = f.listing(db, tenant_id)
    template = category_template(db)
    f.version(db, tenant_id, listing_id, category_template_id=template)
    expect_sqlstate(
        db,
        "23503",
        "DELETE FROM category_templates WHERE id = %s",
        (template,),
        constraint="fk_listing_versions_category_template_id_category_templates",
    )
