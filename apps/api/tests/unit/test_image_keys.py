"""S17b T3 (D1; AC2, AC6): the image key literal and its exact parser."""

from __future__ import annotations

import uuid

import pytest
from app.services.image_keys import ImageKey, image_object_key, parse_image_key
from hypothesis import given
from hypothesis import strategies as st

T = uuid.UUID("11111111-2222-3333-4444-555555555555")
P = uuid.UUID("66666666-7777-8888-9999-000000000000")
I = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")  # noqa: E741
GOOD = f"{T}/{P}/images/{I}"


def test_image_object_key_literal() -> None:
    assert image_object_key(T, P, I) == (
        "11111111-2222-3333-4444-555555555555/66666666-7777-8888-9999-000000000000/images/"
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )
    with pytest.raises(ValueError, match="UUIDs"):
        image_object_key("../x", P, I)  # type: ignore[arg-type]


def test_parse_round_trip() -> None:
    assert parse_image_key(GOOD) == ImageKey(T, P, I)


@pytest.mark.parametrize(
    "key",
    [
        pytest.param(GOOD.upper(), id="uppercase"),
        pytest.param(f"{{{T}}}/{P}/images/{I}", id="braces"),
        pytest.param(f"{T.hex}/{P}/images/{I}", id="no_dashes"),
        pytest.param(f"{GOOD}/x", id="extra_segment"),
        pytest.param(f"{GOOD}/", id="trailing_slash"),
        pytest.param(f"{GOOD}.jpg", id="ext_jpg"),
        pytest.param(f"{T}/{P}/imports/{I}", id="other_folder"),
        pytest.param(f"{T}/../{P}/images/{I}", id="dotdot"),
        pytest.param(f"/{GOOD}", id="leading_slash"),
        pytest.param(f"{GOOD}\n", id="newline_suffix"),
        pytest.param(GOOD.replace("1", chr(0x0661), 1), id="unicode_digit"),
        pytest.param("", id="empty"),
    ],
)
def test_parse_rejects(key: str) -> None:
    assert parse_image_key(key) is None


@given(st.uuids(), st.uuids(), st.uuids())
def test_parse_build_inverse(t: uuid.UUID, p: uuid.UUID, i: uuid.UUID) -> None:
    assert parse_image_key(image_object_key(t, p, i)) == ImageKey(t, p, i)


@given(st.text(max_size=200))
def test_parse_never_accepts_dotdot_or_slash_runs(text: str) -> None:
    parsed = parse_image_key(text)
    if parsed is not None:
        assert ".." not in text and "//" not in text and "\n" not in text
        assert text == image_object_key(parsed.tenant_id, parsed.product_id, parsed.image_id)
