"""SI9 T10 (AC11; CP-D11, CP-D12): the per-key rejected-photo marker
(`products.photo_rejections`). A reject of an unconfirmed photo value records it, a seller value or
a confirmation clears it, and the next photo read never writes or asks it again."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from psycopg.types.json import Jsonb

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


# ---- SI9 T11 (AC12; D8, D12; CP-D15): the reject re-validates the product's listings ----


def _put_content(pw: PhotoWorld, listing_id: Any, **content: Any) -> None:
    """Rewrite the current version's content (set-up only, guards off)."""
    version = pw.world.listing(listing_id)["current_version_id"]
    with pw.conn.transaction():
        pw.conn.execute("SET LOCAL session_replication_role = replica")
        for name, value in content.items():
            pw.conn.execute(
                "UPDATE listing_versions SET content_json = jsonb_set(content_json, %s, %s) "
                "WHERE id = %s",
                [["content", name], Jsonb(value), version],
            )


def _status(pw: PhotoWorld, listing_id: Any) -> tuple[str, str | None]:
    row = pw.world.listing(listing_id)
    return row["status"], row["status_reason"]


def _mismatch(pw: PhotoWorld, listing_id: Any) -> list[tuple[Any, ...]]:
    return list(
        pw.conn.execute(
            "SELECT field_path, severity, status, suggestion FROM listing_issues "
            "WHERE listing_id = %s AND error_code = 'FACT_MISMATCH' ORDER BY field_path",
            [listing_id],
        ).fetchall()
    )


async def _listing_saying(
    pw: PhotoWorld, monkeypatch: pytest.MonkeyPatch, status: str, **content: Any
) -> tuple[Any, str]:
    from .generation_support import set_status

    accepted, value = await _photo_neckline(pw, monkeypatch)
    content = {"description": "A soft cotton kurta.", **content}
    filled = {k: (v.format(value=value) if isinstance(v, str) else v) for k, v in content.items()}
    if "bullets" in filled:
        filled["bullets"] = [b.format(value=value) for b in filled["bullets"]]
    _put_content(pw, accepted.listing_id, **filled)
    set_status(pw.world, accepted.listing_id, status)
    return accepted, value


@pytest.mark.parametrize("status", ["needs_review", "approved"])
async def test_reject_blocks_a_listing_whose_title_uses_the_value(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    pw = photo_world
    accepted, _ = await _listing_saying(
        pw, monkeypatch, status, title="Classic {value} Cotton Kurta"
    )
    if status == "approved":
        with pw.conn.transaction():
            pw.conn.execute("SET LOCAL session_replication_role = replica")
            pw.conn.execute(
                "UPDATE listings SET approved_version_id = current_version_id WHERE id = %s",
                [accepted.listing_id],
            )
    await seller_patch(pw, {"attributes": {"neckline": None}})
    assert _status(pw, accepted.listing_id) == ("blocked", "validation_errors")
    assert pw.world.listing(accepted.listing_id)["approved_version_id"] is None
    assert _mismatch(pw, accepted.listing_id) == [
        (
            "content.title",
            "error",
            "open",
            "Regenerate the listing, or edit the text to remove it.",
        )
    ]


async def test_reject_bullet_only_hit_warns_and_approve_still_works(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .photo_support import approve

    pw = photo_world
    accepted, _ = await _listing_saying(
        pw,
        monkeypatch,
        "validated",
        title="Classic Cotton Kurta",
        bullets=["Soft cotton", "A {value} cut", "Easy care"],
    )
    await seller_patch(pw, {"attributes": {"neckline": None}})
    assert _status(pw, accepted.listing_id) == ("needs_review", None)
    assert [(f, s) for f, s, *_ in _mismatch(pw, accepted.listing_id)] == [
        ("content.bullets[1]", "warning")
    ]
    outcome = await approve(pw, accepted.listing_id)
    assert outcome.http_status == 200, outcome


async def test_reject_keeps_an_approved_listing_on_a_warning_only_check(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a WARNING allows approval, so a reject whose re-check adds only warnings keeps the approval
    pw = photo_world
    accepted, _ = await _listing_saying(
        pw,
        monkeypatch,
        "approved",
        title="Classic Cotton Kurta",
        bullets=["Soft cotton", "A {value} cut", "Easy care"],
    )
    with pw.conn.transaction():
        pw.conn.execute("SET LOCAL session_replication_role = replica")
        pw.conn.execute(
            "UPDATE listings SET approved_version_id = current_version_id WHERE id = %s",
            [accepted.listing_id],
        )
    await seller_patch(pw, {"attributes": {"neckline": None}})
    assert _status(pw, accepted.listing_id) == ("approved", None)
    assert pw.world.listing(accepted.listing_id)["approved_version_id"] is not None
    assert [(f, s) for f, s, *_ in _mismatch(pw, accepted.listing_id)] == [
        ("content.bullets[1]", "warning")
    ]


async def test_reject_leaves_a_listing_that_does_not_use_the_value(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    accepted, _ = await _listing_saying(pw, monkeypatch, "validated", title="Plain Cotton Kurta")
    await seller_patch(pw, {"attributes": {"neckline": None}})
    assert _status(pw, accepted.listing_id) == ("validated", None)
    assert _mismatch(pw, accepted.listing_id) == []


async def test_reject_skips_a_generating_listing(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    accepted, _ = await _listing_saying(
        pw, monkeypatch, "generating", title="Classic {value} Cotton Kurta"
    )
    await seller_patch(pw, {"attributes": {"neckline": None}})
    assert _status(pw, accepted.listing_id)[0] == "generating"
    assert _mismatch(pw, accepted.listing_id) == []


async def test_editing_the_value_out_of_the_title_clears_the_flag(
    api: httpx.AsyncClient, photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    accepted, _ = await _listing_saying(
        pw, monkeypatch, "needs_review", title="Classic {value} Cotton Kurta"
    )
    await seller_patch(pw, {"attributes": {"neckline": None}})
    row = pw.world.listing(accepted.listing_id)
    content = pw.world.scalar(
        "SELECT content_json FROM listing_versions WHERE id = %s", [row["current_version_id"]]
    )
    headers = pw.world.headers("operator@acme.test")
    headers.pop("Idempotency-Key")
    headers["If-Match"] = f'"{row["current_version_id"]}"'
    response = await api.patch(
        f"/api/v1/listings/{accepted.listing_id}/content",
        headers=headers,
        json={"content": {**content["content"], "title": "Classic Cotton Kurta"}},
    )
    assert response.status_code == 200, response.text
    assert [s for _f, _sev, s, _ in _mismatch(pw, accepted.listing_id)] == ["resolved"]
