"""S17b T14b, wired by S19 (DW3, AC3, AC17; PLAN D4; C8): `approve_batch` calls the pure
`evaluate_image_gate` with the stored reports of the version's images (one SELECT per batch):
an unchecked, failed, deleted, foreign or other-marketplace image blocks the approval (item 422
`IMAGE_NON_COMPLIANT`, nothing written, no maker-checker denial); vision findings only warn."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from mkos_schemas.enums import ImageSlot, Marketplace
from mkos_schemas.images import ImageCheck, ImageIssue, ImageReport

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .images_support import image_row
from .products_support import statements
from .review_support import (
    OPERATOR,
    OWNER,
    REVIEWER,
    Made,
    audits,
    decisions,
    generated,
    item,
    only_item,
    post_approve,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
WHEN = datetime(2026, 10, 7, tzinfo=UTC)


def _report(
    image_id: uuid.UUID,
    market: str = "amazon",
    *,
    passed: bool = True,
    issues: int = 0,
    vision_failed: bool = False,
) -> ImageReport:
    checks = [ImageCheck(check="format", method="deterministic", passed=passed)]
    if vision_failed:
        checks.append(ImageCheck(check="text_overlay", method="vision", passed=False))
    return ImageReport(
        image_id=image_id,
        marketplace=Marketplace(market),
        slot=ImageSlot.MAIN,
        pass_=passed,
        checks=checks,
        issues=[
            ImageIssue(code="RESOLUTION_LOW", check="resolution", suggestion="Upload 1000 px.")
            for _ in range(issues)
        ],
        processed_at=WHEN,
    )


async def _store(world: GenWorld, slug: str, image_id: uuid.UUID, *reports: ImageReport) -> None:
    from app.repositories.uow import tenant_uow

    async with tenant_uow(world.tenant(slug)) as uow:
        await uow.images.write_report(
            image_id, reports={r.marketplace: r for r in reports}, meta=None, content_hash="00"
        )


def _image(world: GenWorld, made: Made, slug: str = "acme", **cols: Any) -> uuid.UUID:
    product = world.listing(made.listing_id)["product_id"] if slug == "acme" else None
    if product is None:
        product = world.scalar(
            "SELECT id FROM products WHERE tenant_id = %s AND parent_product_id IS NULL LIMIT 1",
            [world.tenant(slug)],
        )
    return image_row(world.conn, world.tenant(slug), product, slot="main", **cols)


def _with_images(
    world: GenWorld,
    made: Made,
    images: list[uuid.UUID],
    pipeline: list[ImageReport] | None = None,
) -> Made:
    """A version naming `images` (a full regeneration by the operator, written as rows by the
    admin login) with a passed run carrying the parent's pins; it becomes current and
    `validated`."""
    conn = world.conn
    new_id = uuid.uuid4()
    refs = [{"image_id": str(i), "slot": "main", "position": n} for n, i in enumerate(images)]
    evidence = [r.model_dump(mode="json", by_alias=True) for r in (pipeline or [])]
    with conn.transaction():
        conn.execute(
            "INSERT INTO listing_versions (id, tenant_id, listing_id, version, content_json, "
            "image_reports_json, generated_by_user_id, origin, parent_version_id, locale, "
            "rules_version, category_template_id) SELECT %s, tenant_id, listing_id, version + 1, "
            "jsonb_set(jsonb_set(content_json, '{images}', %s::jsonb), '{version}', "
            "to_jsonb(version + 1)), %s::jsonb, %s, 'regeneration', id, locale, rules_version, "
            "category_template_id FROM listing_versions WHERE id = %s",
            [new_id, json.dumps(refs), json.dumps(evidence), world.user(OPERATOR), made.version_id],
        )
        conn.execute(
            "INSERT INTO validation_runs (tenant_id, listing_id, listing_version_id, marketplace, "
            "source, passed, error_report_json, attempt, rules_version, category_template_id, "
            "template_version, facts_digest, blocking_count, warning_count) SELECT tenant_id, "
            "listing_id, %s, marketplace, source, true, error_report_json, attempt, "
            "rules_version, category_template_id, template_version, facts_digest, 0, 0 "
            "FROM validation_runs WHERE listing_version_id = %s AND source = 'rules_engine' "
            "AND passed ORDER BY created_at DESC LIMIT 1",
            [new_id, made.version_id],
        )
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(
            "UPDATE listings SET current_version_id = %s, status = 'validated', "
            "status_reason = NULL WHERE id = %s",
            [new_id, made.listing_id],
        )
    return Made(made.listing_id, new_id, made.job_id)


def _blocked(result: dict[str, Any]) -> list[dict[str, Any]]:
    assert (result["http_status"], result["error"]["error_code"]) == (422, "IMAGE_NON_COMPLIANT")
    return result["error"]["errors"]  # type: ignore[no-any-return]


def _untouched(world: GenWorld, made: Made) -> None:
    row = world.listing(made.listing_id)
    assert (row["status"], row["approved_version_id"]) == ("validated", None)
    assert decisions(world, made.listing_id) == 0
    assert audits(world, made.listing_id, "listing.approved") == 0


async def test_approve_blocked_unchecked_image(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    made = _with_images(gen_world, made, [_image(gen_world, made)])  # pending: no report
    errors = _blocked(only_item(await post_approve(api, gen_world, REVIEWER, [item(made)])))
    assert [(e["field"], e["severity"]) for e in errors] == [("images[0]", "error")]
    _untouched(gen_world, made)


async def test_approve_blocked_failed_image(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    image = _image(gen_world, made)
    await _store(gen_world, "acme", image, _report(image, passed=False, issues=2))
    made = _with_images(gen_world, made, [image])
    errors = _blocked(only_item(await post_approve(api, gen_world, REVIEWER, [item(made)])))
    assert len(errors) == 2 and all(e["raw_code"] == "RESOLUTION_LOW" for e in errors)
    _untouched(gen_world, made)


async def test_approve_vision_warning_only(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    image = _image(gen_world, made)
    await _store(gen_world, "acme", image, _report(image))
    made = _with_images(gen_world, made, [image], [_report(image, vision_failed=True)])
    result = only_item(await post_approve(api, gen_world, REVIEWER, [item(made)]))
    assert result["http_status"] == 200, result
    assert gen_world.listing(made.listing_id)["status"] == "approved"


async def test_approve_passed_image_approves(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    image = _image(gen_world, made)
    await _store(gen_world, "acme", image, _report(image))
    made = _with_images(gen_world, made, [image])
    assert (
        only_item(await post_approve(api, gen_world, REVIEWER, [item(made)]))["http_status"] == 200
    )


async def test_approve_no_images_gate_noop(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    with statements() as seen:
        result = only_item(await post_approve(api, gen_world, REVIEWER, [item(made)]))
    assert result["http_status"] == 200
    assert not [s for s in seen if "FROM images" in s]


async def test_approve_other_marketplace_report_blocks(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    image = _image(gen_world, made)
    await _store(gen_world, "acme", image, _report(image, "flipkart"))
    made = _with_images(gen_world, made, [image])
    _blocked(only_item(await post_approve(api, gen_world, REVIEWER, [item(made)])))


async def test_approve_deleted_image_blocks(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    image = _image(gen_world, made)
    await _store(gen_world, "acme", image, _report(image))
    gen_world.conn.execute("UPDATE images SET deleted_at = now() WHERE id = %s", [image])
    made = _with_images(gen_world, made, [image])
    _blocked(only_item(await post_approve(api, gen_world, REVIEWER, [item(made)])))


async def test_approve_image_gate_batch_partial(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    ok1, bad, ok3 = [await generated(gen_world) for _ in range(3)]
    bad = _with_images(gen_world, bad, [_image(gen_world, bad)])
    response = await post_approve(api, gen_world, REVIEWER, [item(ok1), item(bad), item(ok3)])
    codes = [i["http_status"] for i in response.json()["items"]]
    assert codes == [200, 422, 200]
    _untouched(gen_world, bad)
    assert gen_world.listing(ok3.listing_id)["status"] == "approved"


async def test_approve_image_block_writes_no_maker_checker_denial(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """The gate runs before maker-checker: the operator (the version's maker) is refused for the
    image, not as a maker, and no denial is audited."""
    made = await generated(gen_world, OWNER)
    made = _with_images(gen_world, made, [_image(gen_world, made)])
    _blocked(only_item(await post_approve(api, gen_world, OWNER, [item(made)])))
    assert audits(gen_world, made.listing_id, "listing.approve_denied") == 0


async def test_approve_image_reports_one_query_per_batch(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    items = []
    for _ in range(5):
        made = await generated(gen_world)
        image = _image(gen_world, made)
        await _store(gen_world, "acme", image, _report(image))
        items.append(item(_with_images(gen_world, made, [image])))
    with statements() as seen:
        response = await post_approve(api, gen_world, REVIEWER, items)
    assert [i["http_status"] for i in response.json()["items"]] == [200] * 5
    assert len([s for s in seen if "FROM images" in s]) == 1


async def test_approve_image_gate_idempotent_replay(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    made = _with_images(gen_world, made, [_image(gen_world, made)])
    key = str(uuid.uuid4())
    first = await post_approve(api, gen_world, REVIEWER, [item(made)], key=key)
    batches = gen_world.count("batches")
    again = await post_approve(api, gen_world, REVIEWER, [item(made)], key=key)
    assert again.content == first.content
    assert gen_world.count("batches") == batches
    _untouched(gen_world, made)


async def test_approve_image_gate_tenant_isolation(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """A bharat image id in acme's version is unchecked for acme: blocked, bharat's report
    never read."""
    made = await generated(gen_world)
    foreign = _image(gen_world, made, slug="bharat")
    await _store(gen_world, "bharat", foreign, _report(foreign))
    made = _with_images(gen_world, made, [foreign])
    errors = _blocked(only_item(await post_approve(api, gen_world, REVIEWER, [item(made)])))
    assert errors[0]["message"] == "image not checked yet"
