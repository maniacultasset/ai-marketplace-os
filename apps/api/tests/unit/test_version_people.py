"""Item 17 (client feedback, 11 Nov 2026): how a listing's versions and history name people."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.repositories.members import member_name
from app.services.listing_reads import version_item, written_by

NOW = datetime(2026, 11, 11, 9, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("display", "email", "erased", "want"),
    [
        ("Priya Sharma", "priya@acme.test", None, "Priya Sharma"),
        ("  Priya  ", "priya@acme.test", None, "Priya"),
        (None, "priya@acme.test", None, "priya@acme.test"),
        ("   ", "priya@acme.test", None, "priya@acme.test"),
        ("Priya", "priya@acme.test", NOW, None),
        (None, "", None, None),
    ],
)
def test_member_name(display: str | None, email: str, erased: object, want: str | None) -> None:
    assert member_name(display, email, erased) == want


@pytest.mark.parametrize(
    ("origin", "want"),
    [("generation", "kuinn"), ("regeneration", "kuinn"), ("edit", "person"), ("import", "person")],
)
def test_written_by(origin: str, want: str) -> None:
    assert written_by(origin) == want


def test_version_item_names_only_known_members() -> None:
    maker, editor, outsider = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    row = SimpleNamespace(
        id=uuid.uuid4(),
        version=2,
        origin="edit",
        created_at=NOW,
        generated_by_user_id=maker,
        edited_by_user_id=editor,
        approved_by_user_id=outsider,
        approved_at=NOW,
        self_approved=False,
    )
    out = version_item(row, {maker: "Ravi", editor: "asha@acme.test"})
    assert out["written_by"] == "person"
    assert out["generated_by"] == {"user_id": maker, "name": "Ravi"}
    assert out["edited_by"] == {"user_id": editor, "name": "asha@acme.test"}
    assert out["approved_by"] == {"user_id": outsider, "name": None}
    assert out["self_approved"] is False


def test_version_item_without_editor_or_approver() -> None:
    maker = uuid.uuid4()
    row = SimpleNamespace(
        id=uuid.uuid4(),
        version=1,
        origin="generation",
        created_at=NOW,
        generated_by_user_id=maker,
        edited_by_user_id=None,
        approved_by_user_id=None,
        approved_at=None,
        self_approved=False,
    )
    out = version_item(row, {})
    assert out["written_by"] == "kuinn" and out["edited_by"] is None and out["approved_by"] is None
