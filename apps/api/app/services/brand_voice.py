"""The brand voice (client feedback item 19): the seller's answers to "Tell Kuinn about your brand".

`BrandVoice` is the one shape, written by `PATCH /brand-profile` and stored as
`brand_profiles.brand_voice` (0030):

    {"audience": str | None, "price_segment": "budget" | "mid" | "premium" | "luxury" | None,
     "positioning": str | None, "differentiators": [str], "words_to_use": [str],
     "words_to_avoid": [str], "claim_limits": [str], "example_copy": str | None}

Every text is trimmed, may not be blank, may hold `\\n`/`\\t` but no other control character,
and is capped; list items are deduplicated case-insensitively in order, and a list is capped.
The text reaches a model prompt (`GenerationContext.brand_profile["brand_voice"]`), so the same
scans as the profile voice run on it: no JSON-shaped secret key and no credential-shaped value
(invariant 6); a refusal never quotes the input.

`stored_voice(model)` is what the column keeps: None when every answer is empty (a skipped
questionnaire stores nothing). `voice_for_prompt(raw)` re-reads the column for the generation
context: a row that no longer parses is dropped (logged by class only), never sent half-read.

The voice shapes tone and emphasis only. Product facts always win: the pipeline never takes a
claim, a number or a material from it.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from mkos_schemas.base import StrictModel, find_seam_secret_key
from pydantic import AfterValidator, BeforeValidator, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from app.core.logging import get_logger
from app.core.text import reject_control_chars
from app.services.brand_chunking import has_credential_value

__all__ = [
    "DESCRIPTION_MAX",
    "VOICE_FIELDS",
    "BrandVoice",
    "DescriptionText",
    "PriceSegment",
    "changed_voice_fields",
    "stored_voice",
    "voice_for_prompt",
]

log = get_logger(__name__)

AUDIENCE_MAX: Final = 300
POSITIONING_MAX: Final = 500
EXAMPLE_MAX: Final = 1500
PHRASE_MAX: Final = 200
WORD_MAX: Final = 60
DIFFERENTIATORS_MAX: Final = 8
WORDS_MAX: Final = 30
CLAIMS_MAX: Final = 15
# `ck_brand_profiles_brand_description` (0001) is the same cap.
DESCRIPTION_MAX: Final = 2000

PriceSegment = Literal["budget", "mid", "premium", "luxury"]
VOICE_FIELDS: Final = (
    "audience",
    "price_segment",
    "positioning",
    "differentiators",
    "words_to_use",
    "words_to_avoid",
    "claim_limits",
    "example_copy",
)
_LISTS: Final = ("differentiators", "words_to_use", "words_to_avoid", "claim_limits")


def _strip(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def _clean(value: str) -> str:
    """No control character but `\\n`/`\\t`, and nothing shaped like a key or a credential."""
    reject_control_chars(value)
    if has_credential_value(value) or find_seam_secret_key(value) is not None:
        raise PydanticCustomError("mkos_invalid", "Text may not hold a key, token or password.")
    return value


Strip = BeforeValidator(_strip)
Clean = AfterValidator(_clean)
AudienceText = Annotated[str, Strip, Field(min_length=1, max_length=AUDIENCE_MAX), Clean]
PositioningText = Annotated[str, Strip, Field(min_length=1, max_length=POSITIONING_MAX), Clean]
ExampleText = Annotated[str, Strip, Field(min_length=1, max_length=EXAMPLE_MAX), Clean]
Phrase = Annotated[str, Strip, Field(min_length=1, max_length=PHRASE_MAX), Clean]
Word = Annotated[str, Strip, Field(min_length=1, max_length=WORD_MAX), Clean]
DescriptionText = Annotated[str, Strip, Field(min_length=1, max_length=DESCRIPTION_MAX), Clean]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for item in items:
        folded = item.casefold()
        if folded not in seen:
            seen.add(folded)
            kept.append(item)
    return kept


class BrandVoice(StrictModel):
    """How Kuinn writes for this brand. Absent answers are empty; nothing here is a fact."""

    audience: AudienceText | None = Field(
        default=None, description="Who buys it, in the seller's words."
    )
    price_segment: PriceSegment | None = Field(default=None, description="Where the price sits.")
    positioning: PositioningText | None = Field(
        default=None, description="How the brand wants to be seen."
    )
    differentiators: Annotated[
        list[Phrase],
        Field(max_length=DIFFERENTIATORS_MAX),
        AfterValidator(_dedupe),
    ] = Field(default_factory=list, description="What makes it different.")
    words_to_use: Annotated[
        list[Word],
        Field(max_length=WORDS_MAX),
        AfterValidator(_dedupe),
    ] = Field(default_factory=list, description="Words the copy should use.")
    words_to_avoid: Annotated[
        list[Word],
        Field(max_length=WORDS_MAX),
        AfterValidator(_dedupe),
    ] = Field(default_factory=list, description="Words the copy must not use.")
    claim_limits: Annotated[
        list[Phrase],
        Field(max_length=CLAIMS_MAX),
        AfterValidator(_dedupe),
    ] = Field(default_factory=list, description="Claims the brand never makes.")
    example_copy: ExampleText | None = Field(
        default=None, description="A short piece of copy the seller likes."
    )

    @model_validator(mode="before")
    @classmethod
    def _lists_not_null(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in _LISTS:
                if key in data and data[key] is None:
                    raise PydanticCustomError("mkos_invalid", "Send [] to clear a list.")
        return data


def stored_voice(voice: BrandVoice | None) -> dict[str, Any] | None:
    """The column value: the full shape, or None when nothing was answered."""
    if voice is None:
        return None
    dumped = voice.model_dump(mode="json")
    return dumped if any(dumped[key] for key in VOICE_FIELDS) else None


def changed_voice_fields(before: Any, after: dict[str, Any] | None) -> list[str]:
    """The voice fields whose stored value moves (the audit row's `brand_voice`)."""
    old = before if isinstance(before, dict) else {}
    new = after or {}
    return [key for key in VOICE_FIELDS if old.get(key) != new.get(key)]


def voice_for_prompt(raw: Any) -> dict[str, Any] | None:
    """The stored voice for `GenerationContext.brand_profile["brand_voice"]`, or None."""
    if raw is None:
        return None
    try:
        return stored_voice(BrandVoice.model_validate(raw))
    except ValidationError:
        # the text may be quoted in the error, so only the class is logged
        log.warning("brand_voice.unreadable", error="ValidationError")
        return None
