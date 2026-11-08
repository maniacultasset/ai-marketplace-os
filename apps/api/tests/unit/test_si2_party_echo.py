"""SI2 fix rounds 2 and 3 (AC5, DPDP `db/models/facts.py:88`): a partial or reformatted
marketplace echo of the manufacturer contact never carries the street or city into an issue text,
whichever attribute the issue names (none, another attribute, or the manufacturer one)."""

from __future__ import annotations

import logging

import pytest
from app.adapters.base import PARTY_REJECTED_MESSAGE, issue_text, party_patterns, party_secrets
from tests.adapters.amazon_support import amazon_adapter, amazon_item
from tests.adapters.flipkart_support import fk_item, flipkart_adapter
from tests.support.fake_marketplace import build_fake

# SELLER_FACTS: "Test Mills Pvt Ltd" at "1 Mill Road, Surat, GJ, 395003, IN"
PARTIAL = "The value 'Test Mills Pvt Ltd, 1 Mill Road, Surat' is invalid."
REFORMATTED = "Invalid value 'TEST MILLS PVT LTD; 1 MILL ROAD / SURAT - 395003'"
LOOSE = "Manufacturer 'TEST MILLS PVT LTD; 1 MILL ROAD / SURAT - 395003' is not registered"


def _clean(text: str) -> None:
    folded = text.casefold()
    for part in ("mill road", "surat", "395003"):
        assert part not in folded, part


def test_party_secrets_cover_parts_and_runs() -> None:
    secrets = party_secrets(amazon_item())
    for wanted in (
        "Test Mills Pvt Ltd",
        "1 Mill Road",
        "Surat",
        "395003",
        "1 Mill Road, Surat",
        "Surat, GJ, 395003",
        "1 Mill Road, Surat, GJ, 395003, IN",
        "Test Mills Pvt Ltd, 1 Mill Road, Surat, GJ, 395003, IN",
    ):
        assert wanted in secrets, wanted
    # short tokens would redact unrelated text
    assert "IN" not in secrets and "GJ" not in secrets
    assert list(secrets) == sorted(secrets, key=lambda s: (-len(s), s))


@pytest.mark.parametrize("echo", [PARTIAL, REFORMATTED])
async def test_amazon_partial_echo_is_not_stored(
    echo: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    app = build_fake()
    app.state.fake.spapi_reject["manufacturer_contact_information"] = ("90220", echo)
    result = await amazon_adapter(app).create_listing(amazon_item())
    (field,) = [f for f in result.fields if f.field == "facts.manufacturer"]
    assert field.status == "failed"
    assert field.error is not None and PARTY_REJECTED_MESSAGE in field.error.message
    _clean(result.model_dump_json() + caplog.text)


@pytest.mark.parametrize("echo", [PARTIAL, REFORMATTED])
async def test_flipkart_partial_echo_is_not_stored(
    echo: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    app = build_fake()
    app.state.fake.fk_reject_attribute["Manufacturer Details"] = ("INVALID_VALUE", echo)
    result = await flipkart_adapter(app).create_listing(fk_item(sku="FK-ECHO"))
    (field,) = [f for f in result.fields if f.field == "facts.manufacturer"]
    assert field.status == "failed"
    assert field.error is not None and PARTY_REJECTED_MESSAGE in field.error.message
    _clean(result.model_dump_json() + caplog.text)


def test_party_patterns_match_loose_echoes() -> None:
    parties = party_secrets(amazon_item())
    patterns = party_patterns(parties)
    assert patterns == party_patterns(parties)  # cached, deterministic
    for echo in ("TEST MILLS PVT LTD", "test_mills-pvt.ltd", "1 MILL ROAD / SURAT", "surat"):
        assert any(p.search(echo) for p in patterns), echo
    # short parts stay out: they would redact unrelated text
    assert not any(p.search("GJ IN") for p in patterns)


def test_issue_text_scrubs_loose_echo_on_other_attributes() -> None:
    parties = party_secrets(amazon_item())
    text = issue_text(LOOSE, ["item_name"], {"item_name": ("title",)}, parties)
    _clean(text)
    assert text.startswith("Manufacturer ") and text.endswith(" is not registered")
    # unrelated text passes through untouched
    assert issue_text("Title is too long", [], {}, parties) == "Title is too long"


@pytest.mark.parametrize("names", [None, ["item_name"]])
async def test_amazon_loose_echo(names: list[str] | None, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    app = build_fake()
    item = amazon_item()
    issue: dict[str, object] = {"code": "90220", "message": LOOSE, "severity": "ERROR"}
    if names:
        issue["attributeNames"] = names
    app.state.fake.spapi_issues[item.seller_sku] = [issue]
    result = await amazon_adapter(app).create_listing(item)
    _clean(result.model_dump_json())


async def test_flipkart_loose_echo(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    app = build_fake()
    app.state.fake.fk_reject_attribute["title"] = ("INVALID_VALUE", LOOSE)
    result = await flipkart_adapter(app).create_listing(fk_item(sku="FK-LOOSE"))
    _clean(result.model_dump_json())
