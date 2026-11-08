"""SI1 T19 (ADR-0026): public names and URLs."""

from __future__ import annotations

import re
import uuid

from app.services.public_images import (
    PUBLIC_CACHE_CONTROL,
    new_public_id,
    public_object_name,
    public_url,
)
from hypothesis import given, settings
from hypothesis import strategies as st

PUBLIC_ID = re.compile(r"^[a-z2-7]{26}$")


def test_ten_thousand_ids_unique_and_well_formed() -> None:
    ids = [new_public_id() for _ in range(10_000)]
    assert len(set(ids)) == 10_000
    assert all(PUBLIC_ID.fullmatch(i) for i in ids)


@settings(max_examples=50)
@given(st.uuids(), st.uuids(), st.uuids())
def test_id_shares_nothing_with_the_row_ids(t: uuid.UUID, p: uuid.UUID, i: uuid.UUID) -> None:
    public_id = new_public_id()
    for value in (t, p, i):
        for part in (value.hex, str(value)):
            assert part not in public_id
            assert public_id not in part


def test_url_shape() -> None:
    assert public_url("https://images.kuinn.in", "a" * 26, "image/jpeg") == (
        "https://images.kuinn.in/" + "a" * 26 + ".jpg"
    )
    assert public_object_name("b" * 26, "image/png") == "b" * 26 + ".png"


def test_cache_control_edge_window_one_day() -> None:
    """V1 hardening #17: a deleted image leaves every cache within a day (the privacy policy):
    one day for browsers and the edge, never `immutable`, and the edge worker agrees."""
    import re
    from pathlib import Path

    assert PUBLIC_CACHE_CONTROL == "public, max-age=86400"
    worker = Path(__file__).resolve().parents[4] / "scripts/deploy/cloudflare/images-edge-worker.js"
    text = worker.read_text(encoding="utf-8")
    ttls = {int(v) for v in re.findall(r"cacheTtl: (\d+)", text)}
    defaults = set(re.findall(r'"public, max-age=(\d+)"', text))
    assert ttls == {86400} and defaults == {"86400"}
