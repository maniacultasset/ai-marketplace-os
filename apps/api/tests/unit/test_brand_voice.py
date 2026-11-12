"""Client feedback item 19: the brand voice model, its stored form, the changed-field list and the
prompt read (`app.services.brand_voice`), and the brand profile body that carries it."""

from __future__ import annotations

from typing import Any

import pytest
from app.api.v1.schemas.brand_profile import BrandProfileIn, BrandProfileOut
from app.services.brand_voice import (
    CLAIMS_MAX,
    DIFFERENTIATORS_MAX,
    VOICE_FIELDS,
    WORDS_MAX,
    BrandVoice,
    changed_voice_fields,
    stored_voice,
    voice_for_prompt,
)
from pydantic import ValidationError

FULL: dict[str, Any] = {
    "audience": "Women 25 to 40 who buy for office wear",
    "price_segment": "mid",
    "positioning": "Everyday handloom, made to last",
    "differentiators": ["Hand-block printed", "Pre-washed cotton"],
    "words_to_use": ["breathable", "easy care"],
    "words_to_avoid": ["cheap"],
    "claim_limits": ["No organic claims"],
    "example_copy": "Soft cotton that keeps its shape.\nWash after wash.",
}


def test_full_voice_round_trips_in_the_exact_shape() -> None:
    stored = stored_voice(BrandVoice.model_validate(FULL))
    assert stored == FULL
    assert tuple(stored) == VOICE_FIELDS


def test_absent_answers_are_empty_and_all_empty_stores_nothing() -> None:
    assert BrandVoice().model_dump() == {
        "audience": None,
        "price_segment": None,
        "positioning": None,
        "differentiators": [],
        "words_to_use": [],
        "words_to_avoid": [],
        "claim_limits": [],
        "example_copy": None,
    }
    assert stored_voice(BrandVoice()) is None
    assert stored_voice(None) is None
    one = stored_voice(BrandVoice(price_segment="premium"))
    assert one is not None and one["price_segment"] == "premium" and one["words_to_use"] == []


def test_text_is_trimmed_and_lists_deduplicated_in_order() -> None:
    voice = BrandVoice.model_validate(
        {"audience": "  students  ", "words_to_use": ["Soft", " soft ", "airy", "SOFT"]}
    )
    assert voice.audience == "students"
    assert voice.words_to_use == ["Soft", "airy"]


@pytest.mark.parametrize(
    "body",
    [
        {"audience": "   "},
        {"audience": "x" * 301},
        {"positioning": "x" * 501},
        {"example_copy": "x" * 1501},
        {"price_segment": "cheap"},
        {"differentiators": ["x" * 201]},
        {"differentiators": ["a"] * 0 + [str(i) for i in range(DIFFERENTIATORS_MAX + 1)]},
        {"words_to_use": [str(i) for i in range(WORDS_MAX + 1)]},
        {"words_to_avoid": ["x" * 61]},
        {"words_to_avoid": [""]},
        {"claim_limits": [str(i) for i in range(CLAIMS_MAX + 1)]},
        {"claim_limits": None},
        {"words_to_use": "soft"},
        {"audience": "bell\x07"},
        {"audience": "carriage\rreturn"},
        {"audience": "nul\x00"},
        {"tone": "warm"},
    ],
)
def test_refused(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        BrandVoice.model_validate(body)


@pytest.mark.parametrize(
    "text",
    [
        "use sk-proj-abcdefghijklmnopqrstuvwxyz",
        "Bearer abcdefghijklmnopqrstu",
        '{"refresh_token": "x"}',
        "AKIA" + "ABCDEFGHIJKLMNOP",
    ],
)
def test_credential_or_secret_key_shaped_text_is_refused_without_echo(text: str) -> None:
    with pytest.raises(ValidationError) as caught:
        BrandVoice.model_validate({"example_copy": text})
    assert "key, token or password" in str(caught.value)
    with pytest.raises(ValidationError):
        BrandVoice.model_validate({"words_to_avoid": [text[:60]]})


def test_newlines_and_tabs_are_kept() -> None:
    assert BrandVoice(example_copy="a\n\tb").example_copy == "a\n\tb"


def test_changed_voice_fields() -> None:
    after = stored_voice(BrandVoice.model_validate(FULL))
    assert changed_voice_fields(None, after) == list(VOICE_FIELDS)
    assert changed_voice_fields(after, after) == []
    moved = {**FULL, "words_to_avoid": ["cheap", "flimsy"], "audience": None}
    assert changed_voice_fields(after, moved) == ["audience", "words_to_avoid"]
    assert changed_voice_fields(after, None) == list(VOICE_FIELDS)
    assert changed_voice_fields("not a dict", None) == []


def test_changed_voice_fields_names_only_answered_fields() -> None:
    one = stored_voice(BrandVoice(price_segment="mid"))
    assert changed_voice_fields(None, one) == ["price_segment"]
    assert changed_voice_fields(one, None) == ["price_segment"]
    assert changed_voice_fields({}, {"words_to_use": []}) == []


def test_full_voice_in_devanagari_fits_the_column_check() -> None:
    """Every cap at its limit with 3-byte characters stays under ck_brand_profiles_brand_voice."""
    import json

    word, phrase = "\u0915" * 60, "\u0916" * 200
    voice = BrandVoice(
        audience="\u0917" * 300,
        price_segment="premium",
        positioning="\u0918" * 500,
        differentiators=[f"{i}{phrase[:-2]}" for i in range(8)],
        words_to_use=[f"{i:02}{word[:-2]}" for i in range(30)],
        words_to_avoid=[f"{i:02}{word[:-2]}" for i in range(30)],
        claim_limits=[f"{i:02}{phrase[:-2]}" for i in range(15)],
        example_copy="\u0919" * 1500,
    )
    stored = stored_voice(voice)
    assert stored is not None
    assert len(json.dumps(stored, ensure_ascii=False).encode()) <= 65536


def test_voice_for_prompt() -> None:
    assert voice_for_prompt(None) is None
    assert voice_for_prompt(FULL) == FULL
    assert voice_for_prompt({}) is None
    # a stored row that no longer parses is dropped whole, never half-read
    assert voice_for_prompt({**FULL, "price_segment": "unknown"}) is None
    assert voice_for_prompt({**FULL, "extra": 1}) is None
    assert voice_for_prompt(["not", "a", "dict"]) is None


def test_profile_body_carries_description_and_voice() -> None:
    body = BrandProfileIn.model_validate(
        {"brand_description": "  Cotton wear from Jaipur. ", "brand_voice": FULL}
    )
    assert body.brand_description == "Cotton wear from Jaipur."
    assert body.brand_voice is not None and body.brand_voice.price_segment == "mid"
    assert body.model_fields_set == {"brand_description", "brand_voice"}
    cleared = BrandProfileIn.model_validate({"brand_description": None, "brand_voice": None})
    assert cleared.brand_voice is None and cleared.model_fields_set == {
        "brand_description",
        "brand_voice",
    }
    assert BrandProfileIn.model_validate({}).model_fields_set == set()


@pytest.mark.parametrize(
    "body",
    [
        {"brand_description": "x" * 2001},
        {"brand_description": " "},
        {"brand_description": "token Bearer abcdefghijklmnopqrstu"},
        {"brand_voice": {"price_segment": "low"}},
        {"brand_voice": "warm"},
        {"website_url": "acme.example"},  # wave A: the website must be an http(s) link
    ],
)
def test_profile_body_refused(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        BrandProfileIn.model_validate(body)


def test_profile_out_defaults_have_no_voice() -> None:
    out = BrandProfileOut().model_dump()
    assert out["brand_voice"] is None and out["brand_description"] is None
