"""Saved photo views (`app.services.image_views`): which copy is signed, the list's main photo
pick, and a read that never fails on an unconfigured store."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.adapters.storage import StorageConfigError
from app.repositories.images import ImageDetail, MainImageRow
from app.services import image_views
from app.services.image_views import pick_main, view_key

T, P, IMG = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def test_view_key_prefers_the_vision_copy() -> None:
    key = view_key(T, P, IMG, has_vision=True, clean_mime_type="image/png")
    assert key == f"{T}/{P}/derived/{IMG}/vision.jpg"


@pytest.mark.parametrize(
    ("mime", "name"), [("image/jpeg", "clean.jpg"), ("image/png", "clean.png")]
)
def test_view_key_falls_back_to_the_clean_copy(mime: str, name: str) -> None:
    assert (
        view_key(T, P, IMG, has_vision=False, clean_mime_type=mime)
        == f"{T}/{P}/derived/{IMG}/{name}"
    )


@pytest.mark.parametrize("mime", [None, "image/webp"])
def test_no_derived_copy_no_view(mime: str | None) -> None:
    """The original upload (`images/<id>`) is never signed: it may still carry EXIF."""
    assert view_key(T, P, IMG, has_vision=False, clean_mime_type=mime) is None


def main(
    product: uuid.UUID, colourway: uuid.UUID | None = None, colour: str | None = None
) -> MainImageRow:
    return MainImageRow(
        product_id=product,
        image_id=uuid.uuid4(),
        colourway_id=colourway,
        colour_key=colour,
        has_vision=True,
        clean_mime_type="image/jpeg",
    )


def test_pick_own_main_first() -> None:
    own, style = uuid.uuid4(), uuid.uuid4()
    rows = [main(style), main(own)]
    assert pick_main(rows, product_id=own, parent_product_id=style, colour=None) == rows[1]


def test_variant_takes_its_colour_then_the_style_level_main() -> None:
    style, child = uuid.uuid4(), uuid.uuid4()
    level, red = main(style), main(style, uuid.uuid4(), "red")
    rows = [level, red]
    assert pick_main(rows, product_id=child, parent_product_id=style, colour="  Red ") == red
    assert pick_main(rows, product_id=child, parent_product_id=style, colour="blue") == level
    assert pick_main(rows, product_id=child, parent_product_id=style, colour=None) == level


def test_variant_never_shows_another_colour() -> None:
    style, child = uuid.uuid4(), uuid.uuid4()
    rows = [main(style, uuid.uuid4(), "red")]
    assert pick_main(rows, product_id=child, parent_product_id=style, colour="blue") is None
    assert pick_main(rows, product_id=uuid.uuid4(), parent_product_id=None, colour=None) is None


class Signer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def presign_get(self, bucket: str, key: str, *, expires_s: int) -> str:
        self.calls.append((bucket, key, expires_s))
        return f"https://store/{bucket}/{key}?X-Amz-Signature=s"


async def test_with_views_signs_derived_copies_only(monkeypatch: pytest.MonkeyPatch) -> None:
    signer = Signer()
    monkeypatch.setattr(image_views, "get_storage", lambda: signer)
    monkeypatch.setattr(image_views, "images_bucket", lambda: "images")
    ready = ImageDetail(IMG, "main", None, 0, "checked", "passed", has_vision=True)
    pending = ImageDetail(uuid.uuid4(), "secondary", None, 1, "uploaded", "pending")
    before = datetime.now(UTC)
    out = await image_views.with_views(T, P, [ready, pending])
    assert out[0].view_url is not None and out[0].view_url.endswith("vision.jpg?X-Amz-Signature=s")
    assert out[0].view_expires_at is not None
    assert out[0].view_expires_at <= before + timedelta(seconds=image_views.VIEW_TTL_S)
    assert out[1].view_url is None and out[1].view_expires_at is None
    assert [c[2] for c in signer.calls] == [image_views.VIEW_TTL_S]


async def test_an_unconfigured_store_answers_without_views(
    monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    def refuse() -> Any:
        raise StorageConfigError("S3_ENDPOINT_URL must use https outside APP_ENV=local")

    monkeypatch.setattr(image_views, "get_storage", refuse)
    ready = ImageDetail(IMG, "main", None, 0, "checked", "passed", has_vision=True)
    (out,) = await image_views.with_views(T, P, [ready])
    assert out.view_url is None
    assert await image_views.sign_keys({}) == {}
