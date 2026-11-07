"""Product request and response models (owner step S12).

`ProductCreate` is the site body (spec L11313-11320, DATABASE.md:1197). Each `attributes` value is
wrapped server-side as a seller `Fact`; an unknown key is 422 `UNKNOWN_FACT_KEY` (S12). `images` are
storage keys or upload ids, never URLs; they become `images` rows (T62 default).

SI1 (PLAN D1-D3): create gains the intake fields and `image_uploads` (keys under a minted `id`),
`ProductPatch` is the edit body (absent = unchanged, `null` clears where allowed), and `ProductOut`
gains the read model. Money is a decimal string (CP11).
"""

from __future__ import annotations

from typing import Annotated, Final, Literal
from uuid import UUID

from mkos_schemas.base import NonBlankStr, Slug, StrictModel, WireModel
from mkos_schemas.enums import ImageSlot
from pydantic import AfterValidator, AwareDatetime, Field, model_validator
from pydantic_core import PydanticCustomError

from app.api.pagination import Page
from app.api.v1.schemas.brand_profile import PartyIn, PartyOut
from app.api.v1.schemas.common import StorageKey
from app.services.intake_values import MONEY_PATTERN, MoneyError, parse_money

MAX_PRODUCT_IMAGES: Final = 50
MAX_PRODUCT_ATTRIBUTES: Final = 200
MAX_ATTRIBUTE_TEXT: Final = 2000
MAX_IMAGE_UPLOADS: Final = 20
MAX_SIZE_CHART_ROWS: Final = 40


def _no_bom(value: str) -> str:
    if "\ufeff" in value:
        raise PydanticCustomError("mkos_invalid", "SKU must not contain whitespace.")
    return value


# The pattern is read as ECMA-262 by JSON Schema validators and JS clients, where `\s` also covers
# U+FEFF; pydantic's Rust regex does not, so the validator closes that gap. A validator, not a new
# pattern: the declared contract stays as S3 froze it.
Sku = Annotated[
    str,
    Field(
        pattern=r"^[^\]\s]{1,64}$",
        description="1-64 characters, no whitespace and no `]` (T72; unique per brand).",
    ),
    AfterValidator(_no_bom),
]
ProductTitle = Annotated[NonBlankStr, Field(max_length=500)]
AttributeKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
AttributeScalar = (
    Annotated[str, Field(max_length=MAX_ATTRIBUTE_TEXT)]
    | bool
    | int
    | Annotated[float, Field(allow_inf_nan=False)]
)


def _money(value: str) -> str:
    try:
        parse_money(value)
    except MoneyError as error:
        raise PydanticCustomError("mkos_invalid", "Money must be above 0.") from error
    return value


def _no_nul(value: str) -> str:
    if "\x00" in value:
        raise PydanticCustomError("mkos_invalid", "Text must not contain NUL.")
    return value


# SI3: a task answer; the attribute text rule plus no NUL (it lands in the evidence snapshot).
AnswerScalar = (
    Annotated[str, Field(max_length=MAX_ATTRIBUTE_TEXT), AfterValidator(_no_nul)]
    | bool
    | int
    | Annotated[float, Field(allow_inf_nan=False)]
)

Money = Annotated[
    str,
    Field(
        pattern=MONEY_PATTERN,
        examples=["1299.00"],
        description="INR as a decimal string, above 0, at most 2 decimals.",
    ),
    AfterValidator(_money),
]
StyleNumber = Annotated[NonBlankStr, Field(max_length=64), AfterValidator(_no_nul)]
Measure = Annotated[float, Field(gt=0, le=500, allow_inf_nan=False)]


class SizeChartRow(StrictModel):
    size: Annotated[NonBlankStr, Field(max_length=20), AfterValidator(_no_nul)]
    chest: Measure | None = None
    waist: Measure | None = None
    hip: Measure | None = None
    length: Measure | None = None
    shoulder: Measure | None = None
    sleeve: Measure | None = None
    inseam: Measure | None = None


class SizeChart(StrictModel):
    unit: Literal["cm", "in"]
    basis: Literal["garment", "body"]
    rows: Annotated[list[SizeChartRow], Field(min_length=1, max_length=MAX_SIZE_CHART_ROWS)]


class ImageUploadIn(StrictModel):
    storage_key: StorageKey
    slot: ImageSlot


class ProductCreate(StrictModel):
    sku: Sku
    title: ProductTitle
    category: Slug
    images: Annotated[list[StorageKey], Field(max_length=MAX_PRODUCT_IMAGES)] = Field(
        default_factory=list
    )
    attributes: Annotated[
        dict[AttributeKey, AttributeScalar], Field(max_length=MAX_PRODUCT_ATTRIBUTES)
    ] = Field(default_factory=dict)
    id: UUID | None = Field(
        default=None,
        description="The product id minted by `POST /images/upload-url`; needs `image_uploads`.",
    )
    style_number: StyleNumber | None = None
    mrp: Money | None = None
    selling_price: Money | None = None
    manufacturer: PartyIn | None = Field(
        default=None, description="Only when it differs from the brand default."
    )
    size_chart: SizeChart | None = None
    image_uploads: Annotated[list[ImageUploadIn], Field(max_length=MAX_IMAGE_UPLOADS)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def _id_with_uploads(self) -> ProductCreate:
        if bool(self.image_uploads) != (self.id is not None):
            raise PydanticCustomError(
                "mkos_invalid", "`id` and `image_uploads` are sent together or not at all."
            )
        return self


_NOT_NULLABLE: Final = ("title", "category")
MAX_CONFIRM_ATTRIBUTES: Final = 32


def _unique_keys(keys: list[str]) -> list[str]:
    if len(set(keys)) != len(keys):
        raise PydanticCustomError("mkos_invalid", "A key appears more than once.")
    return keys


class ProductPatch(StrictModel):
    """A JSON merge patch: an absent field is unchanged; `null` clears `style_number`, `mrp`,
    `selling_price`, `size_chart`, `manufacturer` (back to the inherited party) and an
    attribute key. `sku` is immutable and not a field."""

    title: ProductTitle | None = None
    category: Slug | None = None
    style_number: StyleNumber | None = None
    mrp: Money | None = None
    selling_price: Money | None = None
    manufacturer: PartyIn | None = None
    size_chart: SizeChart | None = None
    attributes: (
        Annotated[
            dict[AttributeKey, AttributeScalar | None], Field(max_length=MAX_PRODUCT_ATTRIBUTES)
        ]
        | None
    ) = None
    confirm_attributes: (
        Annotated[
            list[AttributeKey],
            Field(min_length=1, max_length=MAX_CONFIRM_ATTRIBUTES),
            AfterValidator(_unique_keys),
        ]
        | None
    ) = Field(
        default=None,
        description="SI3: photo-derived attribute keys the seller confirms as they are.",
    )

    @model_validator(mode="after")
    def _not_null(self) -> ProductPatch:
        for name in (*_NOT_NULLABLE, "attributes", "confirm_attributes"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise PydanticCustomError("mkos_invalid", f"`{name}` cannot be null.")
        return self


class FibreOut(WireModel):
    fibre: str
    percent: float


class ProductImageOut(WireModel):
    image_id: UUID
    slot: ImageSlot
    colourway_id: UUID | None = None
    position: int
    status: str
    compliance_status: Literal["pending", "passed", "failed"]


class ColourwayOut(WireModel):
    id: UUID
    colour: str
    colour_sku: str | None = None


class PhotoFactOut(WireModel):
    """SI3: an attribute value read from the product photos, shown with a "from photo" badge until
    the seller confirms or changes it. It never backs a claim while unconfirmed."""

    key: str
    value: AttributeScalar
    confidence: float
    image_id: UUID
    confirmed: bool
    confirmed_at: AwareDatetime | None = None


class ProductOut(WireModel):
    id: UUID
    sku: Sku
    title: ProductTitle
    status: Literal["draft", "active"]
    category: Slug
    attributes: dict[str, AttributeScalar] = Field(default_factory=dict)
    images: list[StorageKey] = Field(default_factory=list)
    parent_product_id: UUID | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    style_number: str | None = None
    mrp: str | None = Field(default=None, examples=["1299.00"])
    selling_price: str | None = Field(default=None, examples=["999.00"])
    currency: str | None = None
    fiber_composition: list[FibreOut] | None = None
    size_chart: SizeChart | None = None
    size_chart_url: str | None = None
    manufacturer: PartyOut | None = Field(
        default=None,
        description="The effective manufacturer (own, style or brand default); null on lists.",
    )
    image_details: list[ProductImageOut] = Field(default_factory=list)
    colourways: list[ColourwayOut] = Field(default_factory=list)
    photo_facts: list[PhotoFactOut] = Field(default_factory=list)


class ProductPage(Page[ProductOut]):
    pass
