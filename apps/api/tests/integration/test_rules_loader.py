"""S14b T6 (AC 7; S14a hand-offs, CD5, C6): `load_rules` resolves the effective RuleSet from the
stored rows, counts disabled rows and the dictionary in `rules_version`, and fails closed."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import text

from .auth_support import World
from .conftest import LOCAL_HOSTS
from .ops_support import attribute_definition, category_attribute
from .products_support import statements
from .seam_support import (
    CATEGORY,
    Conn,
    add_rule,
    add_template,
    banned,
    max_length,
    pattern,
    revision_of,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


def _uow(world: World, slug: str = "acme") -> Any:
    from app.repositories.uow import tenant_uow

    return tenant_uow(world.tenant(slug), user_id=world.user(f"owner@{slug}.test"))


async def _load(world: World, slug: str = "acme", category: str = CATEGORY) -> Any:
    from app.services.rules_loader import load_rules

    async with _uow(world, slug) as uow:
        return await load_rules(
            uow, tenant_id=world.tenant(slug), marketplace="amazon", category=category
        )


async def test_rules_loader_platform_plus_own_only(products_world: World, auth_db: Conn) -> None:
    platform = add_rule(auth_db, max_length())
    own = add_rule(auth_db, banned(["cheap"]), tenant_id=products_world.tenant("acme"))
    add_rule(auth_db, banned(["costly"]), tenant_id=products_world.tenant("bharat"))
    add_template(auth_db)
    loaded = await _load(products_world)
    assert {r.id for r in loaded.ruleset.rules} == {platform, own}
    assert loaded.ruleset.marketplace.value == "amazon" and loaded.ruleset.category == CATEGORY
    assert loaded.template is not None and loaded.template_id is not None


async def test_rules_loader_disabled_counts_in_rules_version(
    products_world: World, auth_db: Conn
) -> None:
    on = add_rule(auth_db, max_length(200))
    before = (await _load(products_world)).ruleset.rules_version
    off = add_rule(auth_db, max_length(150), enabled=False, version=2)
    loaded = await _load(products_world)
    assert [r.id for r in loaded.ruleset.rules] == [on]  # the disabled row never runs
    assert loaded.ruleset.rules_version >= revision_of(auth_db, off) > before


async def test_rules_loader_tenant_never_shadows_platform(
    products_world: World, auth_db: Conn
) -> None:
    from . import factories as f

    platform = add_rule(auth_db, banned(["cheap"], policy_ref=f.POLICY_REF))
    own = add_rule(auth_db, banned(["cheap"]), tenant_id=products_world.tenant("acme"))
    loaded = await _load(products_world)
    assert {r.id for r in loaded.ruleset.rules} == {platform, own}


async def test_rules_loader_unsafe_pattern_raises(products_world: World, auth_db: Conn) -> None:
    from app.services.rules_loader import RuleLoadError

    # passes the contract gate (single quantifiers), refused by the engine's overlap guard
    add_rule(auth_db, pattern(r"\s*\s*x"), field="content.title", subject="sku_shape")
    with pytest.raises(RuleLoadError, match="regex guard"):
        await _load(products_world)


async def test_rules_loader_contract_invalid_row_raises(
    products_world: World, auth_db: Conn
) -> None:
    from app.services.rules_loader import RuleLoadError

    add_rule(auth_db, pattern(r"(a+)+"), field="content.title", subject="sku_shape")
    with pytest.raises(RuleLoadError, match="no longer validates"):
        await _load(products_world)


def _record(**over: Any) -> Any:
    from app.repositories.rules import RuleRecord
    from mkos_schemas.rules import Rule

    rule = Rule.model_validate(
        {
            "id": str(uuid.uuid4()),
            "marketplace": "amazon",
            "category": CATEGORY,
            "field": "attributes.color",
            "rule_def": {"type": "required_attribute", "attribute": "color", "message": "m"},
        }
    )
    return replace(RuleRecord(rule=rule, revision=1, tenant_id=None, subject="color"), **over)


def test_rules_loader_subject_mismatch_raises() -> None:
    from app.services.rules_loader import RuleLoadError, check_records

    tenant = uuid.uuid4()
    assert len(check_records([_record()], tenant_id=tenant)) == 1
    with pytest.raises(RuleLoadError, match="subject"):
        check_records([_record(subject="size")], tenant_id=tenant)


def test_rules_loader_foreign_record_raises() -> None:
    from app.services.rules_loader import RuleLoadError, check_records

    with pytest.raises(RuleLoadError, match="another tenant"):
        check_records([_record(tenant_id=uuid.uuid4())], tenant_id=uuid.uuid4())


async def test_rules_loader_tenant_system_code_raises(products_world: World, auth_db: Conn) -> None:
    from app.services.rules_loader import RuleLoadError

    add_rule(
        auth_db,
        banned(["cheap"], error_code="INTERNAL_ERROR"),
        tenant_id=products_world.tenant("acme"),
    )
    with pytest.raises(RuleLoadError, match="error code"):
        await _load(products_world)
    # the other tenant never loads acme's row, so its run is unaffected
    assert (await _load(products_world, "bharat")).ruleset.rules == []


def _bump_definition(conn: Conn, key: str) -> None:
    """A criticality flip, as the migrator would make it (the revision trigger bumps it)."""
    conn.execute(
        "UPDATE attribute_definitions SET is_critical = true, confirm_agent_captures = true "
        "WHERE key = %s",
        [key],
    )


async def test_rules_loader_dictionary_revision_moves_version(
    products_world: World, auth_db: Conn
) -> None:
    add_rule(auth_db, max_length())
    key = attribute_definition(
        auth_db, key=f"s14b_{uuid.uuid4().hex[:12]}", allowed_sources=["seller"]
    )
    category_attribute(auth_db, key, category=CATEGORY)
    other = f"s14b_other_{uuid.uuid4().hex[:8]}"
    category_attribute(auth_db, key, category=other)

    start = (await _load(products_world)).ruleset.rules_version
    _bump_definition(auth_db, key)  # a key this category may not even list
    after_definition = (await _load(products_world)).ruleset.rules_version
    assert after_definition > start

    auth_db.execute(
        "UPDATE category_attributes SET enabled = NOT enabled WHERE category = %s AND "
        "attribute_key = %s",
        [CATEGORY, key],
    )
    after_toggle = (await _load(products_world)).ruleset.rules_version
    assert after_toggle > after_definition

    auth_db.execute(
        "UPDATE category_attributes SET enabled = NOT enabled WHERE category = %s AND "
        "attribute_key = %s",
        [other, key],
    )
    assert (await _load(products_world)).ruleset.rules_version == after_toggle


async def test_rules_loader_template_none_when_absent(products_world: World, auth_db: Conn) -> None:
    add_rule(auth_db, max_length())
    loaded = await _load(products_world)
    assert loaded.template is None and loaded.template_id is None
    assert len(loaded.ruleset.rules) == 1


async def test_rules_loader_three_queries(products_world: World, auth_db: Conn) -> None:
    from app.services.rules_loader import load_rules

    add_rule(auth_db, max_length())
    add_template(auth_db)
    tenant = products_world.tenant("acme")
    async with _uow(products_world) as uow:
        await uow.session.execute(text("SELECT 1"))  # the GUC statements run first
        with statements() as seen:
            await load_rules(uow, tenant_id=tenant, marketplace="amazon", category=CATEGORY)
    assert len(seen) == 3, seen
