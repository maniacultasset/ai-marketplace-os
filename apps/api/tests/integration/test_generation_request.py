"""S18 T9 (DW1, AC 1, 2; D1-D3; CP12): the generate request service, on a new or a `created`
listing, against the demo seed as `app_rw`."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import Marketplace

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import CATEGORY, GenWorld, gen_world, set_status
from .products_support import statements

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
OPERATOR = "operator@acme.test"


async def _request(
    world: GenWorld,
    items: list[Any],
    *,
    email: str = OPERATOR,
    key: str | None = None,
) -> list[Any]:
    from app.repositories.uow import tenant_uow
    from app.services.generation import request_generation

    principal = world.principal(email)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        return await request_generation(uow, principal, key or str(uuid.uuid4()), items)


def _item(world: GenWorld, sku: str = "KURTA-002", **kw: Any) -> Any:
    from app.services.generation import GenerateItem

    return GenerateItem(product_id=world.product("acme", sku), marketplace=Marketplace.AMAZON, **kw)


async def _one(world: GenWorld, item: Any, **kw: Any) -> Any:
    from app.services.generation import ItemRefused

    [result] = await _request(world, [item], **kw)
    if isinstance(result, ItemRefused):
        raise result.error
    return result


async def test_generate_new_listing_one_tx(gen_world: GenWorld) -> None:
    from app.repositories.jobs import job_idempotency_key

    key = str(uuid.uuid4())
    accepted = await _one(gen_world, _item(gen_world), key=key)
    listing = gen_world.listing(accepted.listing_id)
    assert (listing["status"], listing["last_event"], listing["category"]) == (
        "generating",
        "generate",
        CATEGORY,
    )
    job = gen_world.conn.execute(
        "SELECT status, kind, idempotency_key, listing_id, created_by, queue_job_id FROM job_runs "
        "WHERE id = %s",
        [accepted.job_id],
    ).fetchone()
    assert job is not None
    assert job[:5] == (
        "queued",
        "listing_generate",
        job_idempotency_key(key, "listing_generate", accepted.listing_id),
        accepted.listing_id,
        gen_world.user(OPERATOR),
    )
    assert gen_world.count("procrastinate_jobs", "id = %s", [int(job[5])]) == 1


async def test_generate_rollback_leaves_nothing(gen_world: GenWorld) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.generation import request_generation

    principal = gen_world.principal(OPERATOR)
    product = gen_world.product("acme", "KURTA-002")
    with pytest.raises(RuntimeError):
        async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
            await request_generation(uow, principal, str(uuid.uuid4()), [_item(gen_world)])
            raise RuntimeError("abort")
    assert gen_world.listing_of(product) is None
    assert gen_world.count("job_runs") == 0
    assert gen_world.count("procrastinate_jobs") == 0


async def test_generate_on_seeded_created_listing(gen_world: GenWorld) -> None:
    from app.db.seed_ops import demo_listing_ids

    accepted = await _one(gen_world, _item(gen_world, "KURTA-001"))
    assert accepted.listing_id == demo_listing_ids()[0]
    assert gen_world.listing(accepted.listing_id)["status"] == "generating"


async def test_generate_statement_order(gen_world: GenWorld) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.generation import request_generation

    principal = gen_world.principal(OPERATOR)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        with statements() as seen:
            await request_generation(uow, principal, str(uuid.uuid4()), [_item(gen_world)])
    writes = [
        s.split()[0] + " " + s.split()[2 if s.startswith("INSERT") else 1]
        for s in seen
        if s.startswith(("INSERT", "UPDATE"))
    ]
    assert writes == [
        "INSERT listings",
        "INSERT job_runs",
        "UPDATE job_runs",  # the queue entry id, after the defer
        "UPDATE listings",
    ]


async def test_generate_busy_409_no_rows(gen_world: GenWorld) -> None:
    from app.core.errors import ConflictError

    first = await _one(gen_world, _item(gen_world))
    with pytest.raises(ConflictError) as caught:
        await _one(gen_world, _item(gen_world))
    assert caught.value.code is ErrorCode.LISTING_BUSY
    assert gen_world.count("job_runs", "listing_id = %s", [first.listing_id]) == 1


@pytest.mark.parametrize(
    ("column", "code"), [("paused", "LISTING_PAUSED"), ("archived", "LISTING_ARCHIVED")]
)
async def test_generate_paused_or_archived_409(gen_world: GenWorld, column: str, code: str) -> None:
    from app.core.errors import ConflictError
    from app.db.seed_ops import demo_listing_ids

    listing = demo_listing_ids()[0]
    gen_world.conn.execute(
        f"UPDATE listings SET {column}_at = now(), {column}_by_user_id = %s WHERE id = %s",  # noqa: S608
        [gen_world.user("owner@acme.test"), listing],
    )
    with pytest.raises(ConflictError) as caught:
        await _one(gen_world, _item(gen_world, "KURTA-001"))
    assert caught.value.code.value == code
    assert gen_world.count("job_runs") == 0


@pytest.mark.parametrize(
    "status", ["validated", "needs_review", "approved", "blocked", "failed", "published"]
)
async def test_generate_other_status_409_invalid_transition(
    gen_world: GenWorld, status: str
) -> None:
    from app.core.errors import ConflictError
    from app.db.seed_ops import demo_listing_ids

    listing = demo_listing_ids()[0]
    set_status(gen_world, listing, status)
    with pytest.raises(ConflictError) as caught:
        await _one(gen_world, _item(gen_world, "KURTA-001"))
    assert caught.value.code is ErrorCode.INVALID_STATUS_TRANSITION
    assert gen_world.count("job_runs") == 0


async def test_generate_variant_child_422(gen_world: GenWorld) -> None:
    from app.core.errors import InputError

    acme = gen_world.tenant("acme")
    style = f.product(
        gen_world.conn, acme, "style", variation_theme="size", size_run=["m"], category=CATEGORY
    )
    with gen_world.conn.transaction():
        child = f.product(
            gen_world.conn,
            acme,
            "child",
            parent=style,
            category=CATEGORY,
            variant_attributes={"size": "m"},
            attributes={"size": f.fact(value="M", normalized="m")},
        )
        f.cell(
            gen_world.conn,
            acme,
            style,
            size_key="m",
            availability="available",
            unavailable_reason=None,
            variant_product_id=child,
        )
    from app.services.generation import GenerateItem

    with pytest.raises(InputError) as caught:
        await _one(gen_world, GenerateItem(product_id=child, marketplace=Marketplace.AMAZON))
    assert caught.value.field == "product_id"


async def test_generate_no_template_422(gen_world: GenWorld) -> None:
    from app.core.errors import InputError

    with pytest.raises(InputError) as caught:
        await _one(gen_world, _item(gen_world, category="no_such_category"))
    assert caught.value.field == "category"
    assert (
        gen_world.count("listings", "product_id = %s", [gen_world.product("acme", "KURTA-002")])
        == 0
    )


async def test_generate_category_mismatch_422(gen_world: GenWorld) -> None:
    from app.core.errors import InputError

    with pytest.raises(InputError) as caught:
        await _one(gen_world, _item(gen_world, "KURTA-001", category="home_decor_lamps"))
    assert caught.value.field == "category"


async def test_generate_unknown_product_404(gen_world: GenWorld) -> None:
    from app.core.errors import NotFoundError
    from app.services.generation import GenerateItem

    with pytest.raises(NotFoundError):
        await _one(gen_world, GenerateItem(product_id=uuid.uuid4(), marketplace=Marketplace.AMAZON))


async def test_generate_soft_deleted_product_404(gen_world: GenWorld) -> None:
    from app.core.errors import NotFoundError

    product = gen_world.product("acme", "KURTA-002")
    gen_world.conn.execute("UPDATE products SET deleted_at = now() WHERE id = %s", [product])
    with pytest.raises(NotFoundError):
        await _one(gen_world, _item(gen_world))


async def test_generate_other_tenant_product_404(gen_world: GenWorld) -> None:
    from app.core.errors import NotFoundError
    from app.services.generation import GenerateItem

    bharat_product = gen_world.product("bharat", "KURTA-002")
    with pytest.raises(NotFoundError):
        await _one(
            gen_world, GenerateItem(product_id=bharat_product, marketplace=Marketplace.AMAZON)
        )
    assert gen_world.count("listings", "product_id = %s", [bharat_product]) == 0


async def test_generate_two_keys_concurrent_one_listing(gen_world: GenWorld) -> None:
    from app.core.errors import ConflictError

    results = await asyncio.gather(
        _one(gen_world, _item(gen_world)), _one(gen_world, _item(gen_world)), return_exceptions=True
    )
    ok = [r for r in results if not isinstance(r, BaseException)]
    busy = [r for r in results if isinstance(r, ConflictError)]
    assert len(ok) == 1 and len(busy) == 1, results
    assert busy[0].code is ErrorCode.LISTING_BUSY
    assert gen_world.count("job_runs") == 1


async def test_generate_batch_savepoint_isolation(gen_world: GenWorld) -> None:
    from app.services.generation import ItemAccepted, ItemRefused

    # item 2 is the same pair as item 1: busy once item 1 holds it
    results = await _request(
        gen_world,
        [
            _item(gen_world, "KURTA-002"),
            _item(gen_world, "KURTA-002"),
            _item(gen_world, "KURTA-001"),
        ],
    )
    assert [type(r) for r in results] == [ItemAccepted, ItemRefused, ItemAccepted]
    assert results[1].error.code is ErrorCode.LISTING_BUSY
    assert gen_world.count("job_runs") == 2
    for accepted in (results[0], results[2]):
        assert gen_world.listing(accepted.listing_id)["status"] == "generating"


async def test_generate_payload_has_instructions_only(gen_world: GenWorld) -> None:
    accepted = await _one(gen_world, _item(gen_world, user_instructions="Lead with silk."))
    payload = gen_world.scalar("SELECT payload_json FROM job_runs WHERE id = %s", [accepted.job_id])
    assert payload == {"user_instructions": "Lead with silk."}


async def test_generate_secret_shaped_instructions_422_no_rows(
    gen_world: GenWorld, caplog: pytest.LogCaptureFixture
) -> None:
    from app.core.errors import InputError

    with pytest.raises(InputError) as caught:
        await _one(
            gen_world, _item(gen_world, user_instructions='please use {"api_key": "x-sentinel-1"}')
        )
    assert caught.value.field == "user_instructions"
    assert "x-sentinel-1" not in str(caught.value)
    assert gen_world.count("job_runs") == 0
    assert gen_world.listing_of(gen_world.product("acme", "KURTA-002")) is None
    assert gen_world.count("procrastinate_jobs") == 0
    assert "x-sentinel-1" not in caplog.text
