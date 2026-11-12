"""Brand profile and business party models (SI1 T5; PLAN D2, AC6).

`PartyIn` mirrors the `business_parties` CHECKs of revision 0001 (`0001_core_tenancy.py`), plus
the fixed list of 36 Indian state codes and no NUL in any string, so a refused value is a 422 and
never a CHECK's 500. Email and phone are accepted on write but never returned on product reads.
Client item 1: `website_url` follows the sign-up form's pattern, cut to the column CHECK's 2000.

Client feedback item 19: the profile also carries `brand_description` and the structured
`brand_voice` (`app.services.brand_voice.BrandVoice` owns the shape and caps). Both shape how
listings are written; product facts always win.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from mkos_schemas.base import NonBlankStr, StrictModel, WireModel
from pydantic import AfterValidator, AwareDatetime, Field, model_validator
from pydantic_core import PydanticCustomError

from app.core.india import STATE_CODES
from app.core.website import WEBSITE_MAX, WEBSITE_PATTERN
from app.services.brand_voice import BrandVoice, DescriptionText, PriceSegment

__all__ = ["BrandProfileIn", "BrandProfileOut", "BrandVoiceOut", "PartyIn", "PartyOut"]


def _state(value: str) -> str:
    if value not in STATE_CODES:
        raise PydanticCustomError("mkos_invalid", "Unknown Indian state code.")
    return value


def _email_octets(value: str) -> str:
    if len(value.encode("utf-8")) > 254:
        raise PydanticCustomError("mkos_invalid", "Email is at most 254 bytes.")
    return value


StateCode = Annotated[
    str,
    Field(
        pattern=r"^[A-Z]{2}$",
        description="ISO 3166-2:IN code without the `IN-` prefix (36 codes in use since 2020).",
    ),
    AfterValidator(_state),
]
PostalCode = Annotated[str, Field(pattern=r"^[1-9][0-9]{5}$", description="Indian PIN code.")]
Gstin = Annotated[str, Field(pattern=r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")]
Email = Annotated[
    str, Field(pattern=r"^[^@\s]+@[^@\s]+$", max_length=254), AfterValidator(_email_octets)
]
Phone = Annotated[str, Field(pattern=r"^[+]?[0-9][0-9 -]{5,19}$")]


def _no_nul(value: str) -> str:
    if "\x00" in value:
        raise PydanticCustomError("mkos_invalid", "Text must not contain NUL.")
    return value


Website = Annotated[
    str,
    Field(
        max_length=WEBSITE_MAX,
        pattern=WEBSITE_PATTERN,
        description="The brand's website, an http(s) link (the sign-up form's rule).",
    ),
    AfterValidator(_no_nul),
]


class PartyIn(StrictModel):
    legal_name: Annotated[NonBlankStr, Field(max_length=300)]
    address_line1: Annotated[NonBlankStr, Field(max_length=300)]
    address_line2: Annotated[NonBlankStr, Field(max_length=300)] | None = None
    city: Annotated[NonBlankStr, Field(max_length=100)]
    state_code: StateCode
    postal_code: PostalCode
    country_code: Literal["IN"] = "IN"
    gstin: Gstin | None = None
    email: Email | None = None
    phone: Phone | None = None

    @model_validator(mode="after")
    def _no_nul(self) -> PartyIn:
        for value in self.__dict__.values():
            if isinstance(value, str) and "\x00" in value:
                raise PydanticCustomError("mkos_invalid", "Text must not contain NUL.")
        return self


class PartyOut(WireModel):
    id: UUID
    legal_name: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state_code: str | None = None
    postal_code: str | None = None
    country_code: str = "IN"
    gstin: str | None = None
    inherited: bool = Field(
        default=False, description="True when the party comes from the brand default."
    )


class BrandProfileIn(StrictModel):
    default_manufacturer: PartyIn | None = Field(
        default=None, description="Absent leaves it unchanged; null clears it."
    )
    website_url: Website | None = Field(
        default=None,
        description="Optional. Absent leaves it unchanged; null clears it. Never sent to a model.",
    )
    brand_description: DescriptionText | None = Field(
        default=None,
        description="What the brand is, in a few lines. Absent leaves it unchanged; null clears it.",
    )
    brand_voice: BrandVoice | None = Field(
        default=None,
        description=(
            "The whole brand voice (it replaces the stored one). Absent leaves it unchanged; "
            "null, or every answer empty, clears it."
        ),
    )


class BrandVoiceOut(WireModel):
    audience: str | None = None
    price_segment: PriceSegment | None = None
    positioning: str | None = None
    differentiators: list[str] = Field(default_factory=list)
    words_to_use: list[str] = Field(default_factory=list)
    words_to_avoid: list[str] = Field(default_factory=list)
    claim_limits: list[str] = Field(default_factory=list)
    example_copy: str | None = None


class BrandProfileOut(WireModel):
    default_manufacturer: PartyOut | None = None
    website_url: str | None = None
    brand_description: str | None = None
    brand_voice: BrandVoiceOut | None = Field(
        default=None, description='Null until the seller answers "Tell Kuinn about your brand".'
    )
    updated_at: AwareDatetime | None = None
