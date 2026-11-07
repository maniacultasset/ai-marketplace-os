"""SI9 T10 (AC11; CP-D11, CP-D12): the per-key rejected-photo marker
(`products.photo_rejections`). A reject of an unconfirmed photo value records it, a seller value or
a confirmation clears it, and the next photo read never writes or asks it again."""

from __future__ import annotations

import json
from typing import Any

import pytest

from .conftest import LOCAL_HOSTS
from .photo_support import (
    PhotoWorld,
    generate,
    photo_fact,
    photo_world,
    regenerable,
    seller_fact,
    seller_patch,
    spy_pipeline,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["photo_world"]


def _marker(pw: PhotoWorld) -> dict[str, Any]:
    row = pw.conn.execute(
        "SELECT photo_rejections FROM products WHERE id = %s", [pw.product_id]
    ).fetchone()
    assert row is not None
    return dict(row[0])


def _set_marker(pw: PhotoWorld, marker: dict[str, str]) -> None:
    pw.conn.execute(
        "UPDATE products SET photo_rejections = %s::jsonb WHERE id = %s",
        [json.dumps(marker), pw.product_id],
    )


async def _photo_neckline(pw: PhotoWorld, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, str]:
    spy_pipeline(pw, monkeypatch)
    accepted = await generate(pw, "photo_ok")
    fact = pw.attributes()["neckline"]
    assert fact["source"] == "ai" and "confirmed_at" not in fact
    return accepted, str(fact["value"])


async def test_reject_records_the_marker_once(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    _accepted, value = await _photo_neckline(pw, monkeypatch)
    audits = len(pw.audit("product.fact_updated"))
    await seller_patch(pw, {"attributes": {"neckline": None}})
    assert "neckline" not in pw.attributes()
    assert _marker(pw) == {"neckline": value}
    assert len(pw.audit("product.fact_updated")) == audits + 1
    # the same PATCH again changes nothing
    await seller_patch(pw, {"attributes": {"neckline": None}})
    assert _marker(pw) == {"neckline": value}
    assert len(pw.audit("product.fact_updated")) == audits + 1


async def test_seller_value_clears_the_marker(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    await _photo_neckline(pw, monkeypatch)
    await seller_patch(pw, {"attributes": {"neckline": None}})
    assert "neckline" in _marker(pw)
    await seller_patch(pw, {"attributes": {"neckline": "round neck"}})
    assert _marker(pw) == {}
    assert pw.attributes()["neckline"]["source"] == "seller"


async def test_confirming_a_later_photo_value_clears_the_marker(
    photo_world: PhotoWorld,
) -> None:
    pw = photo_world
    _set_marker(pw, {"neckline": "v-neck"})
    pw.put_fact("neckline", photo_fact("round neck", pw.images["main"]))
    await seller_patch(pw, {"confirm_attributes": ["neckline"]})
    assert _marker(pw) == {}


@pytest.mark.parametrize("kind", ["seller", "confirmed"])
async def test_reject_of_a_seller_or_confirmed_value_is_a_plain_clear(
    photo_world: PhotoWorld, kind: str
) -> None:
    pw = photo_world
    fact = (
        seller_fact("round neck")
        if kind == "seller"
        else photo_fact(
            "round neck",
            pw.images["main"],
            confirmed_by=str(pw.world.user("operator@acme.test")),
            confirmed_at="2026-11-07T00:00:00.000000Z",
        )
    )
    pw.put_fact("neckline", fact)
    await seller_patch(pw, {"attributes": {"neckline": None}})
    assert "neckline" not in pw.attributes()
    assert _marker(pw) == {}


async def test_next_read_of_the_rejected_value_writes_nothing(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    accepted, value = await _photo_neckline(pw, monkeypatch)
    await seller_patch(pw, {"attributes": {"neckline": None}})
    tasks = len(pw.tasks())
    regenerable(pw, accepted.listing_id)
    pw.images["lifestyle2"] = pw.add_image("lifestyle")  # a new photo set, so the photos are read
    await generate(pw, "photo_ok")
    assert "neckline" not in pw.attributes()
    assert _marker(pw) == {"neckline": value}
    assert len(pw.tasks()) == tasks


async def test_a_different_read_still_fills(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    _set_marker(pw, {"neckline": "zzz never read"})
    spy_pipeline(pw, monkeypatch)
    await generate(pw, "photo_ok")
    assert pw.attributes()["neckline"]["source"] == "ai"
