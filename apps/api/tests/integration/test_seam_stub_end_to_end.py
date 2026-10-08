"""S14b T9 (AC 2; Done-when 1): sockets off, no GCP or model credentials, the stub
`run_listing_pipeline(ctx, deps)` returns a valid `GenerationResult` with every port going through
the backend implementation, against the demo seed and the real Rules Engine."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import pytest

from .conftest import LOCAL_HOSTS
from .jobs_support import reset_queue
from .seam_support import CATEGORY, Conn

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
PORTS = (
    "retrieve_brand_chunks",
    "validate_rules",
    "validate_images",
    "get_model_credentials",
    "emit_node_event",
)


class Spy:
    """Delegates every port to the backend deps and records which ports ran."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls: list[str] = []

    async def retrieve_brand_chunks(
        self, tenant_id: uuid.UUID, query: str, k: int = 8, types: Sequence[str] | None = None
    ) -> Any:
        self.calls.append("retrieve_brand_chunks")
        return await self.inner.retrieve_brand_chunks(tenant_id, query, k, types)

    async def validate_rules(self, listing: Any) -> Any:
        self.calls.append("validate_rules")
        return await self.inner.validate_rules(listing)

    async def validate_images(self, listing: Any) -> Any:
        self.calls.append("validate_images")
        return await self.inner.validate_images(listing)

    async def get_model_credentials(
        self, purpose: Literal["generation", "vision", "embedding"]
    ) -> Any:
        self.calls.append("get_model_credentials")
        return await self.inner.get_model_credentials(purpose)

    async def emit_node_event(self, node: Any, status: Any, detail: Any = None) -> None:
        self.calls.append("emit_node_event")
        await self.inner.emit_node_event(node, status, detail)


@dataclass
class Seam:
    conn: Conn
    tenant: uuid.UUID
    user: uuid.UUID
    product: uuid.UUID
    listing: uuid.UUID
    job: uuid.UUID


def _scalar(conn: Conn, query: str, params: list[Any]) -> Any:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


@pytest.fixture
async def seam(auth_db: Conn, monkeypatch: pytest.MonkeyPatch) -> Seam:
    from app.cli import run_seed
    from app.core.config import get_settings
    from app.db import seed_ops
    from app.db.seed_ops import seed_tenant_id
    from app.repositories.jobs import EnqueueSpec
    from app.repositories.uow import tenant_uow
    from mkos_schemas.enums import JobKind

    reset_queue(auth_db)
    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GCP_PROJECT_ID", "KMS_KEY_URI"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    assert await run_seed("demo", reset=False) == 0
    tenant = seed_tenant_id("acme")
    user = _scalar(auth_db, "SELECT id FROM users WHERE email = 'operator@acme.test'", [])
    product = _scalar(
        auth_db, "SELECT id FROM products WHERE tenant_id = %s AND sku = 'KURTA-001'", [tenant]
    )
    listing = seed_ops.demo_listing_ids()[0]  # the demo seed's KURTA-001 x amazon listing (T19)
    spec = EnqueueSpec(
        kind=JobKind.LISTING_GENERATE,
        entity_type="listing",
        entity_id=listing,
        listing_id=listing,
        created_by=user,
    )
    async with tenant_uow(tenant, user_id=user) as uow:
        job = (await uow.job_runs.enqueue(spec)).job.id
    async with tenant_uow(tenant, user_id=user) as uow:
        assert await uow.job_runs.claim(job, 300, request_id="req-s14b-seam") is not None
    get_settings.cache_clear()
    return Seam(auth_db, tenant, user, product, listing, job)


def _counts(conn: Conn) -> dict[str, int]:
    from app.db.base import metadata

    out: dict[str, int] = {}
    for name in sorted(metadata.tables):
        out[name] = int(_scalar(conn, f"SELECT count(*) FROM {name}", []))  # noqa: S608
    out["procrastinate_jobs"] = int(_scalar(conn, "SELECT count(*) FROM procrastinate_jobs", []))
    return out


def _job_hash(conn: Conn) -> str:
    cur = conn.execute("SELECT * FROM job_runs ORDER BY id")
    names = [d.name for d in cur.description or ()]
    rows = [{n: v for n, v in zip(names, r, strict=True) if n != "node"} for r in cur.fetchall()]
    return hashlib.sha256(repr(rows).encode()).hexdigest()


async def _run(seam: Seam, instructions: str | None = None) -> tuple[Any, Spy]:
    from app.core.config import get_settings
    from app.repositories.uow import tenant_uow
    from app.services.pipeline_deps import build_pipeline_deps
    from app.services.rules_loader import load_rules
    from mkos_pipeline import run_listing_pipeline
    from mkos_schemas.jobs import GenerationContext

    settings = get_settings()
    assert settings.openai_api_key is None and settings.kms_key_uri is None
    async with tenant_uow(seam.tenant, user_id=seam.user) as uow:
        loaded = await load_rules(
            uow, tenant_id=seam.tenant, marketplace="amazon", category=CATEGORY
        )
    assert loaded.template is not None
    ctx = GenerationContext.model_validate(
        {
            "request_id": "req-s14b-seam",
            "job_id": seam.job,
            "tenant_id": seam.tenant,
            "product_id": seam.product,
            "listing_id": seam.listing,
            "marketplace": "amazon",
            "category": CATEGORY,
            "product": {"title": "Cotton Kurta Set"},
            "brand_profile": {},
            "marketplace_rules": loaded.ruleset,
            "category_template": loaded.template.for_generation(),
            "user_instructions": instructions,
        }
    )
    deps = await build_pipeline_deps(
        settings=settings,
        tenant_id=seam.tenant,
        job_id=seam.job,
        user_id=seam.user,
        product_id=seam.product,
        marketplace=ctx.marketplace,
        rules=loaded,
    )
    spy = Spy(deps)
    return await run_listing_pipeline(ctx, spy), spy


async def test_seam_stub_end_to_end(seam: Seam) -> None:
    from mkos_schemas.jobs import GenerationResult

    before, jobs_before = _counts(seam.conn), _job_hash(seam.conn)
    result, spy = await _run(seam)
    assert GenerationResult.model_validate_json(result.model_dump_json(by_alias=True)) == result
    assert result.terminal_state == "generated", result.error
    assert set(spy.calls) == set(PORTS)
    node = _scalar(seam.conn, "SELECT node FROM job_runs WHERE id = %s", [seam.job])
    assert node == "quality_check"
    # S16 CP12: each query embed of `retrieve_brand_chunks` writes its one `agent_runs` row
    # (invariant 7); the run writes nothing else.
    embeds = spy.calls.count("retrieve_brand_chunks")
    assert embeds >= 1
    assert _counts(seam.conn) == before | {"agent_runs": before["agent_runs"] + embeds}
    assert _job_hash(seam.conn) == jobs_before


async def test_seam_stub_blocked_end_to_end(seam: Seam) -> None:
    from mkos_schemas.codes import ErrorCode

    result, _ = await _run(seam, "stub:blocked")
    assert result.terminal_state == "blocked"
    assert result.attempts_used == 3
    assert result.error is not None
    assert ErrorCode.FIELD_TOO_LONG in {e.error_code for e in result.error.errors}
    title = next(e for e in result.error.errors if e.error_code is ErrorCode.FIELD_TOO_LONG)
    assert title.field == "content.title" and title.rule_id is not None
