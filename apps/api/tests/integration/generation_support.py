"""The S18 generation test world: the demo seed (acme and bharat, one member per role, KURTA-001/002
with current Amazon and Flipkart templates, the seeded RuleSets, brand chunks and the `created`
KURTA-001 x amazon listing with version 1) on an emptied test database, the app under test as
`app_rw`, an empty queue and no model or cloud key. Rows are read back on the admin connection.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest

from .jobs_support import reset_queue

Conn = psycopg.Connection[tuple[Any, ...]]
CATEGORY = "ethnic_wear_kurta_sets"
ROLES = ("owner", "operator", "reviewer", "admin")


@dataclass
class GenWorld:
    conn: Conn
    tenants: dict[str, uuid.UUID] = field(default_factory=dict)
    users: dict[str, uuid.UUID] = field(default_factory=dict)

    def tenant(self, slug: str) -> uuid.UUID:
        return self.tenants[slug]

    def user(self, email: str) -> uuid.UUID:
        return self.users[email]

    def product(self, slug: str, sku: str) -> uuid.UUID:
        row = self.conn.execute(
            "SELECT id FROM products WHERE tenant_id = %s AND sku = %s", [self.tenant(slug), sku]
        ).fetchone()
        assert row is not None, sku
        return row[0]  # type: ignore[no-any-return]

    def principal(self, email: str) -> Any:
        from app.core.rbac import Principal

        slug = email.split("@", 1)[1].split(".", 1)[0]
        return Principal(
            user_id=self.user(email),
            tenant_id=self.tenant(slug),
            role=email.split("@", 1)[0],  # type: ignore[arg-type]
            email=email,
            jti=str(uuid.uuid4()),
        )

    def headers(self, email: str, key: str | None = None) -> dict[str, str]:
        from .auth_support import bearer, tokens_for

        slug = email.split("@", 1)[1].split(".", 1)[0]
        out = bearer(tokens_for(self, email, slug))  # type: ignore[arg-type]
        out["Idempotency-Key"] = key or str(uuid.uuid4())
        return out

    def scalar(self, query: str, params: list[Any] | None = None) -> Any:
        row = self.conn.execute(query, params or []).fetchone()
        return None if row is None else row[0]

    def listing(self, listing_id: uuid.UUID) -> dict[str, Any]:
        cur = self.conn.execute("SELECT * FROM listings WHERE id = %s", [listing_id])
        names = [d.name for d in cur.description or ()]
        row = cur.fetchone()
        assert row is not None
        return dict(zip(names, row, strict=True))

    def listing_of(self, product_id: uuid.UUID, marketplace: str = "amazon") -> uuid.UUID | None:
        return self.scalar(  # type: ignore[no-any-return]
            "SELECT id FROM listings WHERE product_id = %s AND marketplace = %s",
            [product_id, marketplace],
        )

    def count(self, table: str, where: str = "true", params: list[Any] | None = None) -> int:
        return int(self.scalar(f"SELECT count(*) FROM {table} WHERE {where}", params))  # noqa: S608


@contextmanager
def no_model_keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from app.core.config import get_settings

    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GCP_PROJECT_ID", "KMS_KEY_URI"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def build_gen_world(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> GenWorld:
    from app.cli import run_seed
    from app.core.config import get_settings
    from app.db.seed_ops import seed_tenant_id
    from app.db.session import dispose_engine
    from tests.support.rate_limits import reset_rate_limits

    reset_queue(conn)
    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GCP_PROJECT_ID", "KMS_KEY_URI"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    assert await run_seed("demo", reset=False) == 0
    await dispose_engine()
    get_settings.cache_clear()
    reset_rate_limits(conn)
    world = GenWorld(conn)
    for slug in ("acme", "bharat"):
        world.tenants[slug] = seed_tenant_id(slug)
    for email, user_id in conn.execute("SELECT email, id FROM users").fetchall():
        world.users[email] = user_id
    return world


@pytest.fixture
async def gen_world(auth_db: Conn, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[GenWorld]:
    from app.db.queue import get_queue_app

    get_queue_app.cache_clear()
    yield await build_gen_world(auth_db, monkeypatch)
    get_queue_app.cache_clear()


async def make_generating(
    world: GenWorld,
    email: str = "operator@acme.test",
    sku: str = "KURTA-002",
    instructions: str | None = None,
    marketplace: str = "amazon",
) -> Any:
    """`request_generation` for one product of the caller's tenant; returns the `ItemAccepted`."""
    from app.repositories.uow import tenant_uow
    from app.services.generation import GenerateItem, ItemAccepted, request_generation
    from mkos_schemas.enums import Marketplace

    principal = world.principal(email)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        [result] = await request_generation(
            uow,
            principal,
            str(uuid.uuid4()),
            [
                GenerateItem(
                    product_id=world.product(email.split("@", 1)[1].split(".", 1)[0], sku),
                    marketplace=Marketplace(marketplace),
                    user_instructions=instructions,
                )
            ],
        )
    assert isinstance(result, ItemAccepted), result
    return result


REASONS = {"blocked": "corrections_exhausted", "failed": "generation_failed"}


def set_status(
    world: GenWorld, listing_id: uuid.UUID, status: str, reason: str | None = None
) -> None:
    """Put a listing straight into `status` (the admin login, guards off for one statement):
    a test arrangement only, never what the app does."""
    with world.conn.transaction():
        world.conn.execute("SET LOCAL session_replication_role = replica")
        if status == "published":
            world.conn.execute(
                "UPDATE listings SET published_version_id = current_version_id, "
                "first_published_at = now(), marketplace_state = 'processing' WHERE id = %s",
                [listing_id],
            )
        world.conn.execute(
            "UPDATE listings SET status = %s, status_reason = %s WHERE id = %s",
            [status, reason or REASONS.get(status), listing_id],
        )
