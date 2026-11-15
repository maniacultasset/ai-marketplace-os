"""G2 D3: which view each sheet photo link fills (`app.services.photo_links`, pure parts), and the
seller sentences."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from app.services import photo_links as svc


def test_the_first_main_link_is_the_front_when_free() -> None:
    assert svc.choose_view(is_main=True, taken=set(), wanted=("back",)) == "front"


def test_a_taken_front_makes_the_main_link_an_extra() -> None:
    assert svc.choose_view(is_main=True, taken={"front"}, wanted=("back",)) is None


def test_other_links_fill_the_wanted_views_in_order() -> None:
    wanted = ("back", "close_up", "lifestyle")
    views = svc.plan_views([True, False, False, False, False], set(), wanted)
    assert views == ["front", "back", "close_up", "lifestyle", None]


def test_a_taken_view_is_skipped_never_replaced() -> None:
    wanted = ("back", "close_up", "lifestyle")
    views = svc.plan_views([True, False, False], {"front", "back"}, wanted)
    assert views == [None, "close_up", "lifestyle"]


def test_no_main_link_means_no_front() -> None:
    assert svc.plan_views([False, False], set(), ("back",)) == ["back", None]


def test_views_switched_off_leave_every_other_link_extra() -> None:
    assert svc.wanted_views("kurtas", views_enabled=False) == ()
    assert svc.plan_views([True, False], set(), ()) == ["front", None]


def test_wanted_views_are_required_or_recommended_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import photo_views

    rows = [
        SimpleNamespace(view="front", requirement="required"),
        SimpleNamespace(view="back", requirement="recommended"),
        SimpleNamespace(view="close_up", requirement="optional"),
        SimpleNamespace(view="size_chart", requirement="required"),
        SimpleNamespace(view="lifestyle", requirement="required"),
    ]
    monkeypatch.setattr(photo_views, "merged", lambda category: rows)
    assert svc.wanted_views("kurtas", views_enabled=True) == ("back", "lifestyle")


def test_taken_views_from_live_photos() -> None:
    live = [("main", None), ("secondary", "back"), ("secondary", None), ("lifestyle", None)]
    assert svc.taken_views(live) == {"front", "back", "lifestyle"}


def _row(**columns: Any) -> Any:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "position": 2,
        "source_column": "additional_images",
        "status": "failed",
        "problem": "not_found",
        "http_status": 404,
        "file_type": None,
        "image_id": None,
        "colourway_id": None,
        "url": "https://cdn.example.com/p.jpg?token=secret",
    }
    return SimpleNamespace(**{**base, **columns})


@pytest.mark.parametrize(
    ("columns", "message"),
    [
        ({}, "The link returned 404. Check the link is right and public."),
        (
            {"problem": "server_error", "http_status": 503},
            "The site had a problem (error 503). Retry later.",
        ),
        (
            {"problem": "unsupported_type", "file_type": "webp"},
            "This photo is a WebP file. Use a JPG or PNG.",
        ),
        (
            {"problem": "not_public", "url": "https://drive.google.com/file/d/abcdefghijk/view"},
            svc.DRIVE_NOT_PUBLIC,
        ),
        (
            {"problem": "not_public"},
            "The link is not public. Share it so anyone with the link can view, then retry.",
        ),
        (
            {"problem": "too_many_pixels"},
            "The photo is too big to read (over 40 megapixels or 20,000 px on a side). "
            "Use a smaller one.",
        ),
        (
            {"status": "skipped", "problem": "removed_by_seller"},
            "You removed this photo earlier, so it was not downloaded again.",
        ),
    ],
)
def test_seller_messages(columns: dict[str, Any], message: str) -> None:
    result = svc.result_view(_row(**columns), {})
    assert result.message == message
    assert "token" not in result.model_dump_json()


def test_import_cap_message_names_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row(status="skipped", problem="import_cap")
    assert "more than 1,000 photo links" in (svc.message_for(row, cap=1000) or "")
    monkeypatch.setattr(
        svc, "get_settings", lambda: SimpleNamespace(sheet_photos_max_per_import=250)
    )
    shown = svc.result_view(row, {})
    assert shown.retryable and "more than 250 photo links" in (shown.message or "")


def test_a_saved_link_shows_its_live_view() -> None:
    image = uuid.uuid4()
    row = _row(status="saved", problem=None, http_status=None, image_id=image, position=1)
    shown = svc.result_view(row, {image: ("secondary", "close_up")})
    assert (shown.status, shown.view, shown.image_id, shown.retryable) == (
        "saved",
        "close_up",
        image,
        False,
    )
    extra = svc.result_view(row, {image: ("secondary", None)})
    assert extra.view == "extra"
    removed = svc.result_view(row, {})
    assert removed.view is None and removed.message == svc.SAVED_THEN_REMOVED


def test_wire_statuses() -> None:
    assert svc.result_view(_row(status="fetching", problem=None), {}).status == "downloading"
    assert svc.result_view(_row(status="queued", problem=None), {}).status == "queued"


def test_link_hash_uses_the_direct_link() -> None:
    share = "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view?usp=sharing"
    direct = "https://drive.google.com/uc?export=download&id=1AbCdEfGhIjKlMnOp"
    assert svc.link_hash(share) == svc.link_hash(direct)
    assert len(svc.link_hash(share)) == 64
