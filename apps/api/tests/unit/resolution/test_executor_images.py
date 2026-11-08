"""SI11 fix round 1 (R1-3; AC15, CP-D23): the executor hands `image_slot` the version's approved
images that passed their stored check and already have a public copy, as their public locators."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from app.services.resolution import run
from mkos_schemas.enums import Marketplace

BASE = "https://img.example.test"


def _image(public: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        public_id="p" + uuid.uuid4().hex[:8] if public else None,
        public_written_at=datetime.now(UTC) if public else None,
        clean_mime_type="image/jpeg",
    )


class _Images:
    def __init__(self, rows: dict[uuid.UUID, Any], passed: dict[uuid.UUID, bool]) -> None:
        self.rows, self.passed = rows, passed

    async def live_reports_for_images(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:
        return {i: {Marketplace.AMAZON: SimpleNamespace(pass_=ok)} for i, ok in self.passed.items()}

    async def get_live(self, image_id: uuid.UUID) -> Any:
        return self.rows.get(image_id)


@pytest.fixture
def hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run, "get_settings", lambda: SimpleNamespace(image_public_base_url=BASE))


async def test_only_passing_images_with_a_public_copy_are_offered(hosted: None) -> None:
    main, second, unchecked, private = (uuid.uuid4() for _ in range(4))
    rows = {main: _image(), second: _image(), unchecked: _image(), private: _image(public=False)}
    passed = {main: True, second: True, unchecked: False, private: True}
    uow: Any = SimpleNamespace(images=_Images(rows, passed))
    listing = SimpleNamespace(marketplace="amazon")
    refs, ok = await run._main_ok_images(uow, listing, [main, second, unchecked, private])
    assert set(refs) == {
        main,
        second,
        unchecked,
    }  # every public copy is a locator the payload sends
    assert [i for i, _url in ok] == [main, second]  # in listing order; `propose` skips the sent one
    assert all(url.startswith(BASE + "/") for _i, url in ok)


async def test_no_hosting_means_no_image_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run, "get_settings", lambda: SimpleNamespace(image_public_base_url=None))
    uow: Any = SimpleNamespace(images=_Images({}, {}))
    assert await run._main_ok_images(
        uow, SimpleNamespace(marketplace="amazon"), [uuid.uuid4()]
    ) == (
        {},
        (),
    )
