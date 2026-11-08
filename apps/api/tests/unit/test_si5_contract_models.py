"""SI5 (ADR-0029) request models: one credential on accept, the token shape, the trial-extension
bounds, the product identifier shape, and the identifier / exemption exclusion."""

from __future__ import annotations

from typing import Any

import pytest
from app.api.v1.schemas.invites import InviteAccept, InviteCreate, InviteTokenIn, PasswordResetIn
from app.api.v1.schemas.products import ProductCreate, ProductIdentifierIn, ProductPatch
from app.api.v1.schemas.tenants import TenantActivate, TenantExtendTrial
from pydantic import ValidationError

TOKEN = "A" * 21 + "_-" + "z9" * 10
PASSWORD = "Kalakar-Weaves-2026!"
GOOGLE = {"handle": "gd_handle", "binding": "b" * 43}


def _locs(error: ValidationError) -> list[tuple[Any, ...]]:
    return [tuple(e["loc"]) for e in error.errors()]


def test_token_fixture_is_43_characters() -> None:
    assert len(TOKEN) == 43
    assert InviteTokenIn.model_validate({"token": TOKEN}).token == TOKEN


@pytest.mark.parametrize(
    "token",
    [
        "A" * 42,
        "A" * 44,
        "A" * 42 + "+",
        "A" * 42 + "/",
        "A" * 42 + "=",
        "",
        " " * 43,
        "A" * 42 + "é",
        "A" * 42 + "\n",
    ],
)
def test_token_shape_refused(token: str) -> None:
    with pytest.raises(ValidationError):
        InviteTokenIn.model_validate({"token": token})
    with pytest.raises(ValidationError):
        PasswordResetIn.model_validate({"token": token, "new_password": PASSWORD})


def test_accept_needs_exactly_one_credential() -> None:
    InviteAccept.model_validate({"token": TOKEN, "password": PASSWORD})
    InviteAccept.model_validate({"token": TOKEN, "google": GOOGLE})
    with pytest.raises(ValidationError):
        InviteAccept.model_validate({"token": TOKEN})
    with pytest.raises(ValidationError):
        InviteAccept.model_validate({"token": TOKEN, "password": PASSWORD, "google": GOOGLE})


def test_accept_display_name_bounds() -> None:
    with pytest.raises(ValidationError):
        InviteAccept.model_validate(
            {"token": TOKEN, "password": PASSWORD, "display_name": "x" * 200}
        )
    accepted = InviteAccept.model_validate(
        {"token": TOKEN, "password": PASSWORD, "display_name": "Meera Iyer"}
    )
    assert accepted.display_name == "Meera Iyer"


def test_invite_email_is_trimmed_and_lowered() -> None:
    body = InviteCreate.model_validate({"email": " A@B.co ", "role": "reviewer"})
    assert body.email == "a@b.co"
    with pytest.raises(ValidationError):
        InviteCreate.model_validate({"email": "a@b.co", "role": "staff"})
    with pytest.raises(ValidationError):
        InviteCreate.model_validate({"email": "\u0430dmin@b.co", "role": "reviewer"})


@pytest.mark.parametrize("days", [0, 91, True, 7.0, 7.5, "7"])
def test_extend_trial_days_refused(days: Any) -> None:
    with pytest.raises(ValidationError):
        TenantExtendTrial.model_validate({"days": days, "reason": "pilot"})


def test_extend_trial_and_activate_accepted() -> None:
    assert TenantExtendTrial.model_validate({"days": 90, "reason": "pilot"}).days == 90
    assert TenantExtendTrial.model_validate({"days": 1, "reason": "pilot"}).days == 1
    with pytest.raises(ValidationError):
        TenantActivate.model_validate({"reason": " "})
    with pytest.raises(ValidationError):
        TenantActivate.model_validate({"reason": "x" * 501})


@pytest.mark.parametrize(
    "value",
    [
        "1234567",
        "123456789012345",
        "12345678a",
        "\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18",
        "1234 5678",
        "",
    ],
)
def test_identifier_value_shape_refused(value: str) -> None:
    with pytest.raises(ValidationError):
        ProductIdentifierIn.model_validate({"type": "ean", "value": value})


def test_identifier_type_is_closed() -> None:
    with pytest.raises(ValidationError):
        ProductIdentifierIn.model_validate({"type": "isbn", "value": "9780306406157"})
    assert ProductIdentifierIn.model_validate({"type": "gtin", "value": "00012345678905"})


def test_patch_exemption_null_refused() -> None:
    with pytest.raises(ValidationError):
        ProductPatch.model_validate({"identifier_exemption": None})
    patch = ProductPatch.model_validate({"product_identifier": None})
    assert "product_identifier" in patch.model_fields_set


@pytest.mark.parametrize("model", [ProductCreate, ProductPatch])
def test_identifier_and_exemption_together_refused(model: Any) -> None:
    body: dict[str, Any] = {
        "product_identifier": {"type": "ean", "value": "4006381333931"},
        "identifier_exemption": True,
    }
    if model is ProductCreate:
        body |= {"sku": "FAB-1", "title": "Kurta", "category": "kurta"}
    with pytest.raises(ValidationError) as caught:
        model.model_validate(body)
    assert ("identifier_exemption",) in _locs(caught.value)
    body["identifier_exemption"] = False
    model.model_validate(body)
