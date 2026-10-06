"""Every write of `python -m app.cli seed` (S9 T12, T13; CANON-ADDENDUM.md A§5). `app/cli.py`
only orchestrates; the SQL lives here, inside `app/db` (repo rule R4).

- Platform rows (`category_templates`, the NULL-tenant `rules` from `rules.seed.json`) go through
  the migrator engine (DATABASE.md:158), upserted on their natural keys.
- Tenant rows go through one `unit_of_work(seed_tenant_id(slug))` per tenant (D16): the tenant
  is inserted with its fixed `uuid5` id, then read back through the scoped repository.
- `reset_seed_tenants` purges the two seed tenants through the database's `purge_tenant`
  ('data', then 'final'; revision 0004_integrity).

Nothing here prints, asserts or logs a password or a DSN.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, get_args

from mkos_schemas.jobs import BrandChunkType
from mkos_schemas.rules import RuleSet
from pydantic import TypeAdapter
from sqlalchemy import Connection, delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.core.config import get_settings
from app.core.passwords import hash_password
from app.core.vectors import hash_vector
from app.db.base import metadata
from app.db.models.audit_brand import BrandChunk
from app.db.models.catalog import Product
from app.db.models.facts import BrandProfile
from app.db.models.platform import CategoryTemplate
from app.db.models.rules import Rule
from app.db.models.tenancy import Tenant, TenantMembership, User
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


def _seller_fact(value: object) -> dict[str, object]:
    return {"value": value, "source": "seller", "captured_via": "form", "updated_at": FACT_TIME}


# ---- platform rows (migrator) ----


def load_rule_sets(path: Path) -> list[RuleSet]:
    adapter: TypeAdapter[list[RuleSet]] = TypeAdapter(list[RuleSet])
    return adapter.validate_json(path.read_bytes())


def _subject(rule_type: str, rule_def: Mapping[str, Any]) -> str:
    """What S7's `rules_subject_matches` CHECK expects for `rule_type`."""
    if rule_type == "image_requirement":
        return f"{rule_def.get('slot')}:{rule_def.get('check')}"
    key = {
        "claim_requires_attribute": "claim",
        "cross_field": "name",
        "pattern": "name",
        "required_attribute": "attribute",
        "allowed_values": "attribute",
        "conditional_required": "attribute",
        "fact_source": "attribute",
        "fact_backed_attribute": "attribute",
    }.get(rule_type)
    value = rule_def.get(key) if key else None
    return str(value) if value is not None else ""


def _platform_rule_rows(rule_sets: Sequence[RuleSet]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rule_set in rule_sets:
        for rule in rule_set.rules:
            data = rule.model_dump(mode="json")
            if data.get("tenant_id") is not None:
                raise ValueError("rules.seed.json holds platform rules only")
            rule_def = data["rule_def"]
            rows.append(
                {
                    "id": uuid.UUID(data["id"]),
                    "tenant_id": None,
                    "marketplace": data["marketplace"],
                    "category": data.get("category"),
                    "field": data["field"],
                    "rule_type": rule_def["type"],
                    "subject": _subject(rule_def["type"], rule_def),
                    "pack": rule_def.get("pack", "core"),
                    "severity": data["severity"],
                    "version": data["version"],
                    "enabled": data["enabled"],
                    "rule_def": rule_def,
                }
            )
    return rows


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
    rule_rows = _platform_rule_rows(load_rule_sets(rules_path))
    if rule_rows:
        # Any unique conflict (the fixed id or `rules_scope_key`) leaves the stored row alone.
        await connection.execute(insert(Rule).values(rule_rows).on_conflict_do_nothing())


async def _migrator_session(connection: AsyncConnection) -> None:
    """Make `migrator` the *session* user for this transaction: 0004's append-only triggers
    check `session_user`, and the local admin login (`--reset` is local only) is a superuser
    that `SET ROLE` would leave as the session user. A migrator login is left as it is."""
    current = (await connection.execute(text("SELECT session_user"))).scalar_one()
    if current != "migrator":
        await connection.execute(text("SET LOCAL SESSION AUTHORIZATION migrator"))


async def reset_seed_tenants(connection: AsyncConnection) -> None:
    """Purge the two seed tenants with the database's own `purge_tenant` ('data', then 'final';
    revision 0004_integrity), then delete any seed tenant row and seed user left, in one
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
                text("SELECT purge_tenant(:t, :mode)"), {"t": tenant_id, "mode": mode}
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


def _content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
