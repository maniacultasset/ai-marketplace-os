"""Every write of `python -m app.cli seed` (S9 T12, T13; CANON-ADDENDUM.md A§5). `app/cli.py`
only orchestrates; the SQL lives here, inside `app/db` (repo rule R4).

- Platform rows (`category_templates`, the NULL-tenant `rules` from `rules.seed.json`) go through
  the migrator engine (DATABASE.md:158), upserted on their natural keys.
- The attribute dictionary goes in with the platform rows: the full `attribute_dictionary.json`
  through `dictionary sync`'s upsert (S13b; the 0007 facts guard reads it), then the two S12 rows
  (`color`, `fabric_type` with alias `material`; CODE-PLAN S12 CP3), inserted only if missing.
- Tenant rows go through one `unit_of_work(seed_tenant_id(slug))` per tenant (D16): the tenant
  is inserted with its fixed `uuid5` id, then read back through the scoped repository.
- `reset_seed_tenants` purges the two seed tenants through the database's `purge_tenant`
  ('data', then 'final'; revision 0004_integrity).

Nothing here prints, asserts or logs a password or a DSN.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, get_args

from mkos_schemas.errors import ErrorReport
from mkos_schemas.jobs import BrandChunkType
from sqlalchemy import Connection, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.core.config import get_settings
from app.core.passwords import hash_password
from app.core.vectors import hash_vector
from app.db.base import metadata
from app.db.models.audit_brand import BrandChunk
from app.db.models.catalog import Product, ProductColourway, VariantMatrixCell
from app.db.models.facts import BrandProfile
from app.db.models.listings import Listing, ListingVersion
from app.db.models.platform import AttributeDefinition, CategoryTemplate
from app.db.models.rules import Rule
from app.db.models.runs import ValidationRun
from app.db.models.tenancy import Tenant, TenantMembership, User
from app.db.platform_loads import (
    ErrorMapCounts,
    ErrorMapIn,
    TemplateCounts,
    TemplateLoad,
    load_dictionary,
    load_error_map_rows,
    load_rule_sets,
    load_template_rows,
    platform_rule_rows,
    sync_rows,
)
from app.db.seed_templates import flipkart_seed_templates, seed_templates
from app.repositories.base import TenantScopedRepository

SEED_NAMESPACE: Final = uuid.UUID("5eed0000-9c1e-4d6a-8a51-0c5a1e5eed00")
# A§5; reachable only under APP_ENV=local (PLAN D9)
SEED_PASSWORD: Final = "devpassword123"  # noqa: S105
ROLES: Final = ("owner", "operator", "reviewer", "admin")
FACT_TIME: Final = "2026-10-05T00:00:00Z"


@dataclass(frozen=True)
class SeedTenant:
    slug: str
    name: str


TENANTS: Final = (SeedTenant("acme", "Acme Apparel"), SeedTenant("bharat", "Bharat Home"))
# Titles per A§5; A§5 names no category for the two demo-only SKUs (not specified).
SHARED_PRODUCTS: Final = (
    ("KURTA-001", "Cotton Kurta Set", "ethnic_wear_kurta_sets"),
    ("KURTA-002", "Silk Kurta Set", "ethnic_wear_kurta_sets"),
    ("DECOR-001", "Brass Table Lamp", "home_decor_lamps"),
)
DEMO_PRODUCTS: Final[Mapping[str, tuple[str, str, str]]] = {
    "acme": ("ACME-900", "Linen Nehru Jacket", "ethnic_wear_jackets"),
    "bharat": ("BH-900", "Jute Floor Rug", "home_decor_rugs"),
}
TEMPLATE_CATEGORIES: Final = {
    "ethnic_wear_kurta_sets": "KURTA",
    "home_decor_lamps": "LAMP",
}
MARKETPLACES: Final = ("amazon", "flipkart")
GUIDELINES: Final[Mapping[str, list[str]]] = {
    "acme": ["Lead with fabric and fit.", "Never claim 'best' or 'guaranteed'."],
    "bharat": ["Name the material first.", "Keep titles under 150 characters."],
}
GLOSSARY: Final[Mapping[str, dict[str, str]]] = {
    "acme": {"kurta set": "kurta with matching bottom and dupatta"},
    "bharat": {"diya": "small oil lamp"},
}


class SeedTenantIdMismatch(RuntimeError):  # noqa: N818 (D13 names the class in the exit line)
    """The seed slug is held by a tenant whose id is not the seed's fixed `uuid5` id."""


def seed_tenant_id(slug: str) -> uuid.UUID:
    return uuid.uuid5(SEED_NAMESPACE, slug)


def seed_user_email(role: str, slug: str) -> str:
    return f"{role}@{slug}.test"


class _TenantRows(TenantScopedRepository[Tenant]):
    model = Tenant


class _Products(TenantScopedRepository[Product]):
    model = Product


class _Listings(TenantScopedRepository[Listing]):
    model = Listing


def _seller_fact(value: object) -> dict[str, object]:
    return {"value": value, "source": "seller", "captured_via": "form", "updated_at": FACT_TIME}


# ---- platform rows (migrator) ----


def _template_rows() -> list[dict[str, Any]]:
    return [
        {
            "marketplace": marketplace,
            "category": category,
            "version": 1,
            "is_current": True,
            "product_type": product_type,
            "source": "manual",
            "attributes": {"required_attributes": [], "optional_attributes": [], "specs": {}},
        }
        for category, product_type in TEMPLATE_CATEGORIES.items()
        for marketplace in MARKETPLACES
    ]


# CODE-PLAN S12 CP3 (DATABASE.md:1166-1170; T32 alias). The dictionary step owns the final values.
ALL_FACT_SOURCES: Final = ("seller", "document", "pim", "marketplace", "derived", "ai")
ATTRIBUTE_DEFINITION_ROWS: Final[tuple[Mapping[str, Any], ...]] = (
    {
        "key": "color",
        "section": "attributes",
        "storage": "attributes",
        "owner_levels": ["colour", "standalone"],
        "value_type": "text",
        "input_aliases": [],
        "is_critical": False,
        "allowed_sources": list(ALL_FACT_SOURCES),
        "min_confidence": Decimal("0.70"),
        "confirm_agent_captures": False,
        "enabled": True,
    },
    {
        "key": "fabric_type",
        "section": "attributes",
        "storage": "attributes",
        "owner_levels": ["style", "standalone"],
        "value_type": "text",
        "input_aliases": ["material"],
        "is_critical": True,
        "allowed_sources": ["seller", "document", "pim"],
        "min_confidence": None,
        "confirm_agent_captures": True,
        "enabled": True,
    },
)


async def seed_attribute_definitions(connection: AsyncConnection) -> None:
    """Insert the S12 dictionary rows on the migrator connection (`attribute_definitions` is
    SELECT-only for the app roles, DATABASE.md:1152). A stored row is left as it is, so a rerun
    never moves its `revision`."""
    await connection.execute(
        insert(AttributeDefinition)
        .values([dict(row) for row in ATTRIBUTE_DEFINITION_ROWS])
        .on_conflict_do_nothing(index_elements=["key"])
    )


def _assume_migrator(connection: Connection) -> None:
    from app.db.migrations import assume_migration_role

    assume_migration_role(connection)


async def seed_platform_rows(connection: AsyncConnection, rules_path: Path) -> None:
    """Idempotent; touches no tenant row and needs no tenant to exist. `connection` is on
    ALEMBIC_DATABASE_URL and switches to `migrator` (DATABASE.md:158)."""
    await connection.run_sync(_assume_migrator)
    await connection.execute(
        insert(CategoryTemplate)
        .values(_template_rows())
        .on_conflict_do_nothing(constraint="ct_version_key")
    )
    rule_rows = platform_rule_rows(load_rule_sets(rules_path))
    if rule_rows:
        # Any unique conflict (the fixed id or `rules_scope_key`) leaves the stored row alone.
        await connection.execute(insert(Rule).values(rule_rows).on_conflict_do_nothing())
    # The whole dictionary first (S13b): the 0007 facts guard reads it for every seeded fact.
    await sync_rows(connection, load_dictionary())
    await seed_attribute_definitions(connection)


def amazon_template_loads() -> list[TemplateLoad]:
    """S20 T5 (C12): Amazon kurta v2, Amazon tshirts v1 and (A2) Amazon lamps v2 as S13 loader
    entries."""
    return [
        TemplateLoad(version=t.version, product_type=t.product_type, template=t.template)
        for t in seed_templates()
    ]


def flipkart_template_loads() -> list[TemplateLoad]:
    """S22 T15 (D7): Flipkart kurta v2, Flipkart tshirts v1 and (A2) Flipkart lamps v2 as S13
    loader entries."""
    return [
        TemplateLoad(version=t.version, product_type=t.product_type, template=t.template)
        for t in flipkart_seed_templates()
    ]


def platform_template_loads() -> list[TemplateLoad]:
    """Every platform template the migrate job loads: the Amazon, then the Flipkart mappings.
    `app/db/seed_data/platform_templates.json` is this list, written by `render_platform_templates`
    (regenerate it with `uv run --frozen python -m app.db.seed_ops`; a meta test checks it)."""
    return amazon_template_loads() + flipkart_template_loads()


def render_platform_templates() -> str:
    """`platform_template_loads()` as the `templates load` file: stable JSON, one trailing
    newline."""
    body = [entry.model_dump(mode="json") for entry in platform_template_loads()]
    return json.dumps(body, indent=2, ensure_ascii=False) + "\n"


async def seed_flipkart_mappings(connection: AsyncConnection) -> TemplateCounts:
    """Insert the Flipkart mappings and promote each over the current version, exactly as
    `seed_amazon_mappings` does for Amazon (S9's v1 rows are never edited; a rerun writes
    nothing). Runs on the caller's migrator transaction."""
    return await load_template_rows(connection, flipkart_template_loads())


async def seed_amazon_mappings(connection: AsyncConnection) -> TemplateCounts:
    """Insert the Amazon mappings and promote each over the current version (the two ordered
    `is_current` UPDATEs, audited with a NULL tenant). S9's v1 rows are never edited; a rerun
    finds the same content and writes nothing. Runs on the caller's migrator transaction."""
    return await load_template_rows(connection, amazon_template_loads())


@dataclass(frozen=True)
class PlatformLoadCounts:
    templates: TemplateCounts
    error_map: ErrorMapCounts


async def platform_load(
    engine: AsyncEngine, *, rules_path: Path, error_map_rows: Sequence[ErrorMapIn]
) -> PlatformLoadCounts:
    """Every platform row a fresh database needs, and no tenant row (S20 T5b; C18): S9's
    platform rows (templates v1, the NULL-tenant rules, the dictionary), then the Amazon
    mappings, then the Flipkart mappings (S22), then the error map. One migrator transaction per part, each upserting on its
    natural key, so a rerun writes and audits nothing. `seed` and `app.cli platform load` (the
    tenant-less acceptance stack) share this one path."""
    async with engine.begin() as connection:
        await seed_platform_rows(connection, rules_path)
    async with engine.begin() as connection:
        amazon = await seed_amazon_mappings(connection)
    async with engine.begin() as connection:
        flipkart = await seed_flipkart_mappings(connection)
    async with engine.begin() as connection:
        error_map = await load_error_map_rows(connection, error_map_rows)
    templates = TemplateCounts(
        inserted=amazon.inserted + flipkart.inserted,
        unchanged=amazon.unchanged + flipkart.unchanged,
        promoted=amazon.promoted + flipkart.promoted,
    )
    return PlatformLoadCounts(templates=templates, error_map=error_map)


async def _migrator_session(connection: AsyncConnection) -> None:
    """Make `migrator` the *session* user for this transaction: 0004's append-only triggers
    check `session_user`, and the local admin login (`--reset` is local only) is a superuser
    that `SET ROLE` would leave as the session user. A migrator login is left as it is."""
    current = (await connection.execute(text("SELECT session_user"))).scalar_one()
    if current != "migrator":
        await connection.execute(text("SET LOCAL SESSION AUTHORIZATION migrator"))


async def reset_seed_tenants(connection: AsyncConnection) -> None:
    """Purge the two seed tenants with the database's own `purge_tenant` ('data', then 'final',
    with `force` past its offboarding and retention preconditions; revision 0004_integrity), then delete any seed tenant row and seed user left, in one
    transaction on the migrator connection. The purge knows every tenant table and passes the
    append-only guards, so this never has to track new tables."""
    await _migrator_session(connection)
    ids = [seed_tenant_id(t.slug) for t in TENANTS]
    tenants, users = metadata.tables["tenants"], metadata.tables["users"]
    for tenant_id in ids:
        found = await connection.execute(select(tenants.c.id).where(tenants.c.id == tenant_id))
        if found.first() is None:
            continue
        for mode in ("data", "final"):
            await connection.execute(
                # force: the seed tenants are live, and --reset runs only under APP_ENV=local
                text("SELECT purge_tenant(:t, :mode, force => true)"),
                {"t": tenant_id, "mode": mode},
            )
    await connection.execute(delete(tenants).where(tenants.c.id.in_(ids)))
    domains = [f"%@{t.slug}.test" for t in TENANTS]
    await connection.execute(delete(users).where(or_(*(users.c.email.ilike(d) for d in domains))))


# ---- tenant rows (one unit_of_work per tenant) ----


async def seed_tenant(session: AsyncSession, tenant: SeedTenant) -> uuid.UUID:
    tenant_id = seed_tenant_id(tenant.slug)
    await session.execute(
        insert(Tenant)
        .values(id=tenant_id, name=tenant.name, slug=tenant.slug, status="active")
        .on_conflict_do_nothing(index_elements=["slug"])
    )
    if await _TenantRows(session, tenant_id).get_by_id(tenant_id) is None:
        raise SeedTenantIdMismatch(tenant.slug)
    return tenant_id


async def seed_users_and_memberships(session: AsyncSession, tenant: SeedTenant) -> int:
    tenant_id = seed_tenant_id(tenant.slug)
    for role in ROLES:
        email = seed_user_email(role, tenant.slug)
        # 0005's RLS: a user row is read and inserted by its login email (users_login and
        # users_signup), as the login and signup flows do; transaction-local.
        await session.execute(select(func.set_config("app.login_email", email.lower(), True)))
        user_id = await _user_id(session, email)
        if user_id is None:
            await session.execute(
                insert(User)
                .values(email=email, hashed_password=hash_password(SEED_PASSWORD))
                .on_conflict_do_nothing(index_elements=[func.lower(User.__table__.c.email)])
            )
            user_id = await _user_id(session, email)
        if user_id is None:
            raise RuntimeError("seed user missing after insert")
        await session.execute(
            insert(TenantMembership)
            .values(tenant_id=tenant_id, user_id=user_id, role=role)
            .on_conflict_do_nothing(constraint="uq_tenant_memberships_tenant_id_user_id")
        )
    await session.execute(select(func.set_config("app.login_email", "", True)))
    return len(ROLES)


async def _user_id(session: AsyncSession, email: str) -> uuid.UUID | None:
    stmt = select(User.id).where(func.lower(User.email) == email.lower())
    found: uuid.UUID | None = (await session.execute(stmt)).scalar_one_or_none()
    return found


def products_for(profile: str, slug: str) -> list[tuple[str, str, str]]:
    products = list(SHARED_PRODUCTS)
    if profile == "demo":
        products.append(DEMO_PRODUCTS[slug])
    return products


async def seed_products(session: AsyncSession, tenant: SeedTenant, profile: str) -> int:
    tenant_id = seed_tenant_id(tenant.slug)
    products = products_for(profile, tenant.slug)
    for sku, title, category in products:
        await session.execute(
            insert(Product)
            .values(
                tenant_id=tenant_id,
                sku=sku,
                title=title,
                category=category,
                provenance={"title": _seller_fact(title), "category": _seller_fact(category)},
            )
            .on_conflict_do_nothing(constraint="products_tenant_sku_idx")
        )
    return len(products)


def brand_chunk_text(slug: str, chunk_type: str) -> str:
    return f"{slug} brand {chunk_type.replace('_', ' ')} guidance (demo seed)"


async def seed_brand_data(session: AsyncSession, tenant: SeedTenant) -> None:
    """Demo only: guidelines, glossary, a brand profile and one chunk per chunk type."""
    tenant_id = seed_tenant_id(tenant.slug)
    await _TenantRows(session, tenant_id).update_one(
        tenant_id,
        guidelines=GUIDELINES[tenant.slug],
        glossary=GLOSSARY[tenant.slug],
    )
    await session.execute(insert(BrandProfile).values(tenant_id=tenant_id).on_conflict_do_nothing())
    model = get_settings().embedding_model
    source_id = uuid.uuid5(SEED_NAMESPACE, f"brand_profile:{tenant.slug}")
    for chunk_type in get_args(BrandChunkType):
        body = brand_chunk_text(tenant.slug, chunk_type)
        await session.execute(
            insert(BrandChunk)
            .values(
                tenant_id=tenant_id,
                type=chunk_type,
                text=body,
                embedding=hash_vector(f"{tenant.slug}:{chunk_type}:0"),
                embedding_model=model,
                content_hash=_content_hash(body),
                source_type="brand_profile",
                source_id=source_id,
                chunk_index=list(get_args(BrandChunkType)).index(chunk_type),
            )
            .on_conflict_do_nothing()
        )


# S19 T22 (CP-D5): acme's one brand rule, local demo only: `stubwarn` in a title is a warning, so
# the stub's `warnings` scenario reaches `needs_review` through the backend's own engine re-check.
WARNING_RULE_TERM: Final = "stubwarn"


def warning_rule_id() -> uuid.UUID:
    return uuid.uuid5(SEED_NAMESPACE, "acme:rule:stubwarn")


async def seed_warning_rule(connection: AsyncConnection) -> None:
    """acme's tenant `banned_terms` warning on `content.title`, by its fixed id (a rerun inserts
    nothing). The admin connection writes it: `app_rw` holds no INSERT on `rules`."""
    rule_def = {
        "type": "banned_terms",
        "terms": [WARNING_RULE_TERM],
        "pack": "brand",
        "error_code": "VALUE_NOT_ALLOWED",
        "message": "The title uses a word the brand avoids.",
        "suggestion": "Remove the word from the title.",
    }
    await connection.execute(
        insert(Rule)
        .values(
            id=warning_rule_id(),
            tenant_id=seed_tenant_id("acme"),
            marketplace="amazon",
            category=None,
            field="content.title",
            rule_type="banned_terms",
            subject="",
            pack="brand",
            severity="warning",
            version=1,
            enabled=True,
            rule_def=rule_def,
        )
        .on_conflict_do_nothing()
    )


def _content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---- the demo listing (S14b T19; D12): one Amazon listing whose title the seeded rule flags ----

DEMO_LISTING_SKU: Final = "KURTA-001"
DEMO_LISTING_MARKETPLACE: Final = "amazon"
DEMO_LISTING_CATEGORY: Final = "ethnic_wear_kurta_sets"
DEMO_TITLE_UNIT: Final = "Cotton Kurta Set for Women with Straight Fit and Round Neck "
DEMO_TITLE: Final = (DEMO_TITLE_UNIT * 4)[:220] + "."  # 221 characters: one over the seeded 200
DEMO_ATTRIBUTES: Final[Mapping[str, str]] = {"color": "indigo", "fabric_type": "cotton"}


def demo_listing_ids() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """(listing, version 1, its rules run): fixed `uuid5` ids, so a rerun inserts nothing."""
    return (
        uuid.uuid5(SEED_NAMESPACE, "acme:demo-listing"),
        uuid.uuid5(SEED_NAMESPACE, "acme:demo-listing:v1"),
        uuid.uuid5(SEED_NAMESPACE, "acme:demo-listing:rules-run-1"),
    )


def demo_listing_content() -> dict[str, Any]:
    """The version's `content_json` (the canonical listing's `content`)."""
    return {
        "title": DEMO_TITLE,
        "bullets": ["Soft cotton fabric that stays comfortable through the day"],
        "description": "A cotton kurta set with a straight fit and a round neck.",
    }


async def demo_listing_product(session: AsyncSession) -> uuid.UUID:
    """acme's KURTA-001, read back through the scoped repository's session."""
    tenant_id = seed_tenant_id("acme")

    repo = _Products(session, tenant_id)
    stmt = repo._select().where(Product.sku == DEMO_LISTING_SKU)
    found = (await session.execute(stmt)).scalar_one()
    return found.id


async def seed_demo_listing(
    session: AsyncSession,
    *,
    report: ErrorReport,
    rules_version: int,
    template_id: uuid.UUID,
    template_version: int,
) -> None:
    """Write the demo listing, its version 1 and the version's rules run under acme's unit of
    work. The report is the engine's own (`app.cli` computes it with the loader and `evaluate`).
    Idempotent: every row has a fixed id and is inserted `ON CONFLICT DO NOTHING`."""
    tenant_id = seed_tenant_id("acme")
    listing_id, version_id, run_id = demo_listing_ids()
    product_id = await demo_listing_product(session)
    operator = await _user_id(session, seed_user_email("operator", "acme"))
    if operator is None:
        raise RuntimeError("the acme operator is missing: seed the tenants first")
    await session.execute(
        insert(Listing)
        .values(
            id=listing_id,
            tenant_id=tenant_id,
            product_id=product_id,
            marketplace=DEMO_LISTING_MARKETPLACE,
            category=DEMO_LISTING_CATEGORY,
        )
        .on_conflict_do_nothing()
    )
    await session.execute(
        insert(ListingVersion)
        .values(
            id=version_id,
            tenant_id=tenant_id,
            listing_id=listing_id,
            version=1,
            content_json=demo_listing_content(),
            generated_by_user_id=operator,
            origin="generation",
            rules_version=rules_version,
            category_template_id=template_id,
        )
        .on_conflict_do_nothing()
    )
    listings = _Listings(session, tenant_id)
    criterion = listings._tenant_criterion(include_platform=False)
    await session.execute(
        listings._scoped_write(
            update(Listing)
            .where(Listing.id == listing_id, Listing.current_version_id.is_(None), criterion)
            .values(current_version_id=version_id)
        )
    )
    await session.execute(
        insert(ValidationRun)
        .values(
            id=run_id,
            tenant_id=tenant_id,
            listing_id=listing_id,
            listing_version_id=version_id,
            marketplace=DEMO_LISTING_MARKETPLACE,
            source="rules_engine",
            passed=report.passed,
            error_report_json=report.model_dump(mode="json"),
            attempt=1,
            rules_version=rules_version,
            category_template_id=template_id,
            template_version=template_version,
            facts_digest=hashlib.sha256(b"{}").digest(),
            blocking_count=len(report.blocking),
            warning_count=len(report.errors) - len(report.blocking),
        )
        .on_conflict_do_nothing()
    )


# ---- S20 T21 (C12): the demo kurta family, pinned to the Amazon kurta mapping (version 2) ----

DEMO_FAMILY_SKU: Final = "KURTA-FAM-01"
DEMO_FAMILY_THEME: Final = "size_color"
DEMO_FAMILY_SIZES: Final = ("s", "m")
DEMO_FAMILY_COLOURS: Final = ("navy blue", "maroon")
DEMO_FAMILY_TEMPLATE_VERSION: Final = 2


def demo_family_child_sku(size: str, colour: str) -> str:
    return f"{DEMO_FAMILY_SKU}-{size.upper()}-{colour.replace(' ', '-').upper()}"


def demo_family_ids() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """(style product, listing, version 1): fixed `uuid5` ids, so a rerun inserts nothing."""
    return (
        uuid.uuid5(SEED_NAMESPACE, "acme:demo-family"),
        uuid.uuid5(SEED_NAMESPACE, "acme:demo-family:listing"),
        uuid.uuid5(SEED_NAMESPACE, "acme:demo-family:listing:v1"),
    )


def demo_family_content() -> dict[str, Any]:
    """The family version's `content_json`: the whole canonical body (DATABASE.md:778). Colour
    and size are the children's axes, so the parent carries only the shared facts."""
    return {
        "content": {
            "title": "Cotton Kurta Set for Women with Straight Fit",
            "bullets": [
                "Soft cotton fabric that stays comfortable through the day",
                "Straight fit with a round neck",
            ],
            "description": "A cotton kurta set with a straight fit, in navy blue and maroon.",
        },
        "keywords": {"search_terms": ["cotton kurta set"], "hidden_terms": ["straight kurta"]},
        "attributes": {"fabric_type": "cotton"},
    }


def _family_fact(value: object, **extra: object) -> dict[str, object]:
    return {**_seller_fact(value), **extra}


async def seed_demo_family(session: AsyncSession) -> None:
    """acme's kurta family (style + 4 children, a complete matrix) and its Amazon listing with
    version 1 pinned to the current Amazon kurta template (version 2, promoted by
    `platform_load` before this runs). One unit of work, so the deferred matrix check sees the
    whole family; every row has a fixed id and is inserted `ON CONFLICT DO NOTHING`."""
    tenant_id = seed_tenant_id("acme")
    style_id, listing_id, version_id = demo_family_ids()
    operator = await _user_id(session, seed_user_email("operator", "acme"))
    if operator is None:
        raise RuntimeError("the acme operator is missing: seed the tenants first")
    template = (
        await session.execute(
            select(CategoryTemplate.id).where(
                CategoryTemplate.marketplace == DEMO_LISTING_MARKETPLACE,
                CategoryTemplate.category == DEMO_LISTING_CATEGORY,
                CategoryTemplate.version == DEMO_FAMILY_TEMPLATE_VERSION,
            )
        )
    ).scalar_one()
    title = "Cotton Kurta Set (family)"
    provenance = {
        "title": _seller_fact(title),
        "category": _seller_fact(DEMO_LISTING_CATEGORY),
    }
    await session.execute(
        insert(Product)
        .values(
            id=style_id,
            tenant_id=tenant_id,
            sku=DEMO_FAMILY_SKU,
            title=title,
            category=DEMO_LISTING_CATEGORY,
            variation_theme=DEMO_FAMILY_THEME,
            size_run=list(DEMO_FAMILY_SIZES),
            provenance=provenance,
        )
        .on_conflict_do_nothing()
    )
    for position, colour in enumerate(DEMO_FAMILY_COLOURS, start=1):
        colourway_id = uuid.uuid5(SEED_NAMESPACE, f"acme:demo-family:colourway:{colour}")
        await session.execute(
            insert(ProductColourway)
            .values(
                id=colourway_id,
                tenant_id=tenant_id,
                style_product_id=style_id,
                attributes={"color": _seller_fact(colour)},
                position=position,
            )
            .on_conflict_do_nothing()
        )
        for size in DEMO_FAMILY_SIZES:
            sku = demo_family_child_sku(size, colour)
            child_id = uuid.uuid5(SEED_NAMESPACE, f"acme:demo-family:child:{sku}")
            await session.execute(
                insert(Product)
                .values(
                    id=child_id,
                    tenant_id=tenant_id,
                    sku=sku,
                    title=f"{title} {size.upper()} {colour}",
                    category=DEMO_LISTING_CATEGORY,
                    parent_product_id=style_id,
                    variant_attributes={"size": size, "color": colour},
                    attributes={"size": _family_fact(size.upper(), normalized=size)},
                    provenance=provenance,
                )
                .on_conflict_do_nothing()
            )
            await session.execute(
                insert(VariantMatrixCell)
                .values(
                    id=uuid.uuid5(SEED_NAMESPACE, f"acme:demo-family:cell:{sku}"),
                    tenant_id=tenant_id,
                    style_product_id=style_id,
                    colourway_id=colourway_id,
                    size_key=size,
                    availability="available",
                    variant_product_id=child_id,
                    source="seller",
                )
                .on_conflict_do_nothing()
            )
    await session.execute(
        insert(Listing)
        .values(
            id=listing_id,
            tenant_id=tenant_id,
            product_id=style_id,
            marketplace=DEMO_LISTING_MARKETPLACE,
            category=DEMO_LISTING_CATEGORY,
        )
        .on_conflict_do_nothing()
    )
    await session.execute(
        insert(ListingVersion)
        .values(
            id=version_id,
            tenant_id=tenant_id,
            listing_id=listing_id,
            version=1,
            content_json=demo_family_content(),
            generated_by_user_id=operator,
            origin="generation",
            category_template_id=template,
        )
        .on_conflict_do_nothing()
    )
    listings = _Listings(session, tenant_id)
    criterion = listings._tenant_criterion(include_platform=False)
    await session.execute(
        listings._scoped_write(
            update(Listing)
            .where(Listing.id == listing_id, Listing.current_version_id.is_(None), criterion)
            .values(current_version_id=version_id)
        )
    )


# ---- S22 T15 (D7; R3): acme's Flipkart demo listings, pinned to the Flipkart kurta mapping ----

DEMO_FLIPKART_MARKETPLACE: Final = "flipkart"
DEMO_FLIPKART_TEMPLATE_VERSION: Final = 2


def demo_flipkart_ids() -> dict[str, tuple[uuid.UUID, uuid.UUID]]:
    """{sku: (listing, version 1)}: fixed `uuid5` ids, so a rerun inserts nothing."""
    return {
        sku: (
            uuid.uuid5(SEED_NAMESPACE, f"acme:demo-flipkart:{sku}"),
            uuid.uuid5(SEED_NAMESPACE, f"acme:demo-flipkart:{sku}:v1"),
        )
        for sku in (DEMO_LISTING_SKU, DEMO_FAMILY_SKU)
    }


def demo_flipkart_standalone_content() -> dict[str, Any]:
    """KURTA-001's Flipkart version: the whole canonical body, with the colour and fabric the
    Flipkart mapping requires (DATABASE.md:778)."""
    return {
        "content": {
            "title": "Cotton Kurta Set for Women with Straight Fit and Round Neck",
            "bullets": ["Soft cotton fabric that stays comfortable through the day"],
            "description": "A cotton kurta set with a straight fit and a round neck.",
        },
        "keywords": {"search_terms": ["cotton kurta set"], "hidden_terms": []},
        "attributes": dict(DEMO_ATTRIBUTES),
    }


async def seed_demo_flipkart(session: AsyncSession) -> None:
    """acme's Flipkart listings for KURTA-001 (standalone) and KURTA-FAM-01 (the 2 x 2 family,
    children only on Flipkart), each with version 1 pinned to the Flipkart kurta mapping
    (version 2, promoted by `platform_load`). Runs after `seed_demo_family` in acme's unit of
    work; every row has a fixed id and is inserted `ON CONFLICT DO NOTHING`."""
    tenant_id = seed_tenant_id("acme")
    operator = await _user_id(session, seed_user_email("operator", "acme"))
    if operator is None:
        raise RuntimeError("the acme operator is missing: seed the tenants first")
    template = (
        await session.execute(
            select(CategoryTemplate.id).where(
                CategoryTemplate.marketplace == DEMO_FLIPKART_MARKETPLACE,
                CategoryTemplate.category == DEMO_LISTING_CATEGORY,
                CategoryTemplate.version == DEMO_FLIPKART_TEMPLATE_VERSION,
            )
        )
    ).scalar_one()
    products = _Products(session, tenant_id)
    contents = {
        DEMO_LISTING_SKU: demo_flipkart_standalone_content(),
        DEMO_FAMILY_SKU: demo_family_content(),
    }
    listings = _Listings(session, tenant_id)
    criterion = listings._tenant_criterion(include_platform=False)
    for sku, (listing_id, version_id) in demo_flipkart_ids().items():
        product_id = (
            await session.execute(
                products._select().where(Product.sku == sku).with_only_columns(Product.id)
            )
        ).scalar_one()
        await session.execute(
            insert(Listing)
            .values(
                id=listing_id,
                tenant_id=tenant_id,
                product_id=product_id,
                marketplace=DEMO_FLIPKART_MARKETPLACE,
                category=DEMO_LISTING_CATEGORY,
            )
            .on_conflict_do_nothing()
        )
        await session.execute(
            insert(ListingVersion)
            .values(
                id=version_id,
                tenant_id=tenant_id,
                listing_id=listing_id,
                version=1,
                content_json=contents[sku],
                generated_by_user_id=operator,
                origin="generation",
                category_template_id=template,
            )
            .on_conflict_do_nothing()
        )
        await session.execute(
            listings._scoped_write(
                update(Listing)
                .where(Listing.id == listing_id, Listing.current_version_id.is_(None), criterion)
                .values(current_version_id=version_id)
            )
        )


if __name__ == "__main__":  # pragma: no cover - the regeneration command, run by hand
    target = Path(__file__).resolve().parent / "seed_data/platform_templates.json"
    target.write_text(render_platform_templates(), encoding="utf-8")
