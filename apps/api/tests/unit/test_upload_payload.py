"""SI12 T7: the upload request hash input. A role-less form hashes exactly as before SI12, so
older clients' Idempotency-Key replays still match; a role changes the hash."""

from __future__ import annotations

import uuid

from app.services.images import ImageUploadForm, upload_payload
from mkos_schemas.enums import ImageSlot

PRODUCT = uuid.UUID("4e8a1b30-77cc-41d9-8f02-5b9c1de3a004")
COLOURWAY = uuid.UUID("6f1d2c3b-8a9e-4b70-9c55-2e4f6a8b0d11")


def _form(**over: object) -> ImageUploadForm:
    fields: dict[str, object] = {
        "product_id": PRODUCT,
        "slot": ImageSlot.SECONDARY,
        "colourway_id": None,
        "storage_key": None,
    }
    fields.update(over)
    return ImageUploadForm(**fields)  # type: ignore[arg-type]


def test_role_less_form_hashes_as_before_si12() -> None:
    assert upload_payload(_form(), b"JPEG") == (
        b"4e8a1b30-77cc-41d9-8f02-5b9c1de3a004\x00secondary\x00\x00\x00JPEG"
    )
    assert upload_payload(
        _form(slot=ImageSlot.MAIN, colourway_id=COLOURWAY, storage_key="t/p/images/i"), None
    ) == (
        b"4e8a1b30-77cc-41d9-8f02-5b9c1de3a004\x00main\x00"
        b"6f1d2c3b-8a9e-4b70-9c55-2e4f6a8b0d11\x00t/p/images/i\x00"
    )


def test_role_changes_the_hash() -> None:
    plain = upload_payload(_form(), b"JPEG")
    back = upload_payload(_form(role="back"), b"JPEG")
    close = upload_payload(_form(role="close_up"), b"JPEG")
    assert len({plain, back, close}) == 3
    assert back == b"4e8a1b30-77cc-41d9-8f02-5b9c1de3a004\x00secondary:back\x00\x00\x00JPEG"


def test_no_file_bytes_impersonate_a_role() -> None:
    # The role is folded into the slot part, never appended after the file.
    assert upload_payload(_form(role="back"), b"X") != upload_payload(_form(), b"X\x00back")
