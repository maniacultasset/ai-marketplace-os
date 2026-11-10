"""Product request and response models (owner step S12).

`ProductCreate` is the site body (spec L11313-11320, DATABASE.md:1197). Each `attributes` value is
wrapped server-side as a seller `Fact`; an unknown key is 422 `UNKNOWN_FACT_KEY` (S12). `images` are
storage keys or upload ids, never URLs; they become `images` rows (T62 default).

SI1 (PLAN D1-D3): create gains the intake fields and `image_uploads` (keys under a minted `id`),
`ProductPatch` is the edit body (absent = unchanged, `null` clears where allowed), and `ProductOut`
gains the read model. Money is a decimal string (CP11).

SI12: the photo `role` (Back, Close-up on a `secondary` image), the seller fields `hsn_code`,
`package` and `country_of_origin`, the size-chart confirm (`size_chart_image_id`), and on detail
reads `field_sources`, `suggestions` and `size_chart_suggestion`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Final, Literal
from uuid import UUID

from mkos_schemas.base import NonBlankStr, Slug, StrictModel, WireModel
from mkos_schemas.enums import ImageRole, ImageSlot
from mkos_schemas.size_chart_read import (
    MAX_SIZE_CHART_MEASURE,
    MAX_SIZE_CHART_READ_ROWS,
    MAX_SIZE_CHART_SIZE_CHARS,
    MAX_SIZE_CHART_UNIT_LINE_CHARS,
)
from pydantic import (
    AfterValidator,
    AwareDatetime,
    Field,
    StrictBool,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from app.api.pagination import Page
from app.api.v1.schemas.brand_profile import PartyIn, PartyOut
from app.api.v1.schemas.common import StorageKey
from app.services.intake_values import (
    MAX_DIMENSION,
    MAX_GRAMS,
    MONEY_PATTERN,
    MoneyError,
    check_dimension,
    check_hsn,
    normalize_country,
    parse_money,
)

MAX_PRODUCT_IMAGES: Final = 50
MAX_PRODUCT_ATTRIBUTES: Final = 200
MAX_ATTRIBUTE_TEXT: Final = 2000
MAX_IMAGE_UPLOADS: Final = 20
MAX_SIZE_CHART_ROWS: Final = MAX_SIZE_CHART_READ_ROWS


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
# SI8: the brand a product lists under (one of the tenant's own brand names) and its audience
BrandName = Annotated[NonBlankStr, Field(max_length=100), AfterValidator(_no_nul)]
Gender = Literal["women", "men", "unisex", "girls", "boys", "baby"]
_BRAND_IN: Final = (
    "SI8: the brand this product lists under, style or standalone level; one of your brand names "
    "(the store name or a brand profile name; 422 VALUE_NOT_ALLOWED otherwise), stored in that "
    "name's spelling. Absent: the brand default applies."
)
_GENDER_IN: Final = (
    "SI8: the audience (style or standalone level); required before an Amazon publish of Kurta "
    "sets and T-shirts."
)
Measure = Annotated[float, Field(gt=0, le=MAX_SIZE_CHART_MEASURE, allow_inf_nan=False)]
_UNIT_REQUIRED: Final = "Choose the size chart's unit, cm or in, before saving."


class SizeChartRow(StrictModel):
    size: Annotated[
        NonBlankStr, Field(max_length=MAX_SIZE_CHART_SIZE_CHARS), AfterValidator(_no_nul)
    ]
    chest: Measure | None = None
    waist: Measure | None = None
    hip: Measure | None = None
    length: Measure | None = None
    shoulder: Measure | None = None
    sleeve: Measure | None = None
    inseam: Measure | None = None


class SizeChart(StrictModel):
    """A saved size chart. The seller always sends the unit: a read never supplies it, and a read
    whose unit is `unknown` must be given `cm` or `in` first (else 422 `VALIDATION_FAILED` at
    `size_chart.unit`)."""

    unit: Literal["cm", "in"] = Field(description="Required: `cm` or `in`, chosen by the seller.")
    basis: Literal["garment", "body"]
    rows: Annotated[list[SizeChartRow], Field(min_length=1, max_length=MAX_SIZE_CHART_ROWS)]

    @field_validator("unit", mode="before")
    @classmethod
    def _unit_chosen(cls, value: object) -> object:
        if value not in ("cm", "in"):
            raise PydanticCustomError("mkos_invalid", _UNIT_REQUIRED)
        return value


class SuggestedSizeChart(WireModel):
    """SI12: a chart as read from a size-chart photo. `unit` is `unknown` when the photo prints no
    unit, or the read unit could not be backed by a printed line: the seller picks one."""

    unit: Literal["cm", "in", "unknown"]
    basis: Literal["garment", "body"]
    rows: Annotated[list[SizeChartRow], Field(min_length=1, max_length=MAX_SIZE_CHART_ROWS)]


def _role_on_secondary(value: ImageRole | None, info: ValidationInfo) -> ImageRole | None:
    """SI12: Back and Close-up are `secondary` images; a role on any other slot is refused."""
    if value is not None and info.data.get("slot") not in (None, ImageSlot.SECONDARY):
        raise PydanticCustomError("mkos_invalid", "A role is only set on a secondary image.")
    return value


_ROLE_DOC: Final = (
    "SI12: `back` or `close_up`, only with `slot: secondary` (Front is `main`). A new holder "
    "takes the role; the old one stays a plain secondary."
)


class ImageUploadIn(StrictModel):
    storage_key: StorageKey
    slot: ImageSlot
    role: ImageRole | None = Field(default=None, description=_ROLE_DOC)

    _role = field_validator("role")(_role_on_secondary)


def _hsn(value: str) -> str:
    try:
        return check_hsn(value)
    except ValueError as exc:
        raise PydanticCustomError("mkos_invalid", str(exc)) from None


def _country(value: str) -> str:
    try:
        return normalize_country(value)
    except ValueError as exc:
        raise PydanticCustomError("mkos_invalid", str(exc)) from None


def _dimension(value: Decimal) -> Decimal:
    try:
        return check_dimension(value)
    except ValueError as exc:
        raise PydanticCustomError("mkos_invalid", str(exc)) from None


# SI12: the import bounds (app/services/intake_values.py, one source with the CSV import)
HsnCode = Annotated[
    str,
    Field(
        pattern=r"^([0-9]{4}|[0-9]{6}|[0-9]{8})$",
        examples=["6109"],
        description="SI12: the HSN code, 4, 6 or 8 digits.",
    ),
    AfterValidator(_hsn),
]
CountryCode = Annotated[
    str,
    Field(
        pattern=r"^[A-Za-z]{2}$",
        examples=["IN"],
        description="SI12: ISO 3166 alpha-2, stored upper-cased.",
    ),
    AfterValidator(_country),
]
Dimension = Annotated[
    Decimal,
    Field(gt=0, le=MAX_DIMENSION, max_digits=7, decimal_places=2, examples=["30.00"]),
    AfterValidator(_dimension),
]
Grams = Annotated[int, Field(ge=1, le=MAX_GRAMS, strict=True, examples=[450])]
_DIMS: Final = ("length_cm", "breadth_cm", "height_cm")


class PackageIn(StrictModel):
    """SI12: the shipping package. A JSON merge patch (CP-D8): a key not sent is unchanged. The
    three dimensions are sent together, all set or all null (null clears them); `weight_g: null`
    clears the weight. `{}` is refused: send `package: null` to clear all four."""

    length_cm: Dimension | None = None
    breadth_cm: Dimension | None = None
    height_cm: Dimension | None = None
    weight_g: Grams | None = None

    @model_validator(mode="after")
    def _dims_together(self) -> PackageIn:
        sent = self.model_fields_set
        if not sent:
            raise PydanticCustomError(
                "mkos_invalid", "Send at least one package field, or `package: null` to clear."
            )
        dims = [name for name in _DIMS if name in sent]
        if dims:
            nulls = [name for name in _DIMS if getattr(self, name) is None]
            if len(dims) != len(_DIMS) or len(nulls) not in (0, len(_DIMS)):
                raise PydanticCustomError(
                    "mkos_invalid",
                    "Send length_cm, breadth_cm and height_cm together, all set or all null.",
                )
        return self


class PackageOut(WireModel):
    length_cm: Decimal | None = None
    breadth_cm: Decimal | None = None
    height_cm: Decimal | None = None
    weight_g: int | None = None


IdentifierType = Literal["gtin", "ean", "upc"]


class ProductIdentifierIn(StrictModel):
    """SI5: the barcode of a variant or a standalone product (never a style). The value is digits
    only with a valid GS1 check digit; `upc` is 12 digits, `ean` 8 or 13, `gtin` 8, 12, 13 or 14."""

    type: IdentifierType
    value: Annotated[str, Field(pattern=r"^[0-9]{8,14}$", examples=["4006381333931"])]


class ProductIdentifierOut(WireModel):
    type: IdentifierType
    value: str


def _identifier_or_exemption(value: bool | None, info: ValidationInfo) -> bool | None:
    """Both a barcode and `identifier_exemption: true` in one body is 422 at the exemption."""
    if value is True and info.data.get("product_identifier") is not None:
        raise PydanticCustomError(
            "mkos_invalid", "Send a product_identifier or identifier_exemption, not both."
        )
    return value


IDENTIFIER_DOC: Final = (
    "SI5: true marks the product GTIN-exempt (clears any barcode); false clears the exemption. "
    "On a style it is set on every variant without a barcode."
)


class ProductCreate(StrictModel):
    sku: Sku
    title: ProductTitle
    category: Slug | None = Field(
        default=None,
        description=(
            "SI14: optional; without it the product is created with no category, and while "
            "`category_suggest_enabled` is on a classification is queued (the seller confirms "
            "the suggestion with PATCH). A listing needs a category first."
        ),
    )
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
    brand_name: BrandName | None = Field(default=None, description=_BRAND_IN)
    gender: Gender | None = Field(default=None, description=_GENDER_IN)
    mrp: Money | None = None
    selling_price: Money | None = None
    manufacturer: PartyIn | None = Field(
        default=None, description="Only when it differs from the brand default."
    )
    size_chart: SizeChart | None = None
    hsn_code: HsnCode | None = None
    package: PackageIn | None = None
    country_of_origin: CountryCode | None = None
    image_uploads: Annotated[list[ImageUploadIn], Field(max_length=MAX_IMAGE_UPLOADS)] = Field(
        default_factory=list
    )
    product_identifier: ProductIdentifierIn | None = None
    identifier_exemption: StrictBool | None = Field(default=None, description=IDENTIFIER_DOC)

    _one_identifier = field_validator("identifier_exemption")(_identifier_or_exemption)

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
    """A JSON merge patch: an absent field is unchanged; `null` clears `style_number`,
    `brand_name` (back to the brand default), `gender`, `mrp`, `selling_price`, `size_chart`,
    `manufacturer` (back to the inherited party), `hsn_code`, `package`, `country_of_origin` and an
    attribute key. `sku` is immutable and not a field."""

    title: ProductTitle | None = None
    category: Slug | None = None
    style_number: StyleNumber | None = None
    brand_name: BrandName | None = Field(default=None, description=_BRAND_IN)
    gender: Gender | None = Field(default=None, description=_GENDER_IN)
    mrp: Money | None = None
    selling_price: Money | None = None
    manufacturer: PartyIn | None = None
    size_chart: SizeChart | None = None
    size_chart_image_id: UUID | None = Field(
        default=None,
        description=(
            "SI12: the live size-chart image this chart was read from (only with a non-null "
            "`size_chart`); stored as the chart's evidence."
        ),
    )
    hsn_code: HsnCode | None = None
    package: PackageIn | None = Field(
        default=None, description="SI12: merges; `null` clears all four values."
    )
    country_of_origin: CountryCode | None = None
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
    product_identifier: ProductIdentifierIn | None = Field(
        default=None, description="SI5: the barcode; `null` clears a stored one."
    )
    identifier_exemption: StrictBool | None = Field(default=None, description=IDENTIFIER_DOC)

    _one_identifier = field_validator("identifier_exemption")(_identifier_or_exemption)

    @field_validator("size_chart_image_id")
    @classmethod
    def _image_with_chart(cls, value: UUID | None, info: ValidationInfo) -> UUID | None:
        # a `size_chart` that failed its own validation is absent here and reports alone
        if value is not None and "size_chart" in info.data and info.data["size_chart"] is None:
            raise PydanticCustomError(
                "mkos_invalid", "`size_chart_image_id` is sent only with a `size_chart`."
            )
        return value

    @model_validator(mode="after")
    def _not_null(self) -> ProductPatch:
        for name in (*_NOT_NULLABLE, "attributes", "confirm_attributes", "identifier_exemption"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise PydanticCustomError("mkos_invalid", f"`{name}` cannot be null.")
        return self


class RecategorizeIn(StrictModel):
    """SI9: the new category of a product that already has listings (never a published one)."""

    category: Slug


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
    # SI7 (MINOR, defaults keep older bodies valid): where the pixels came from, the image it was
    # made from, and the candidate kinds the seller may ask for on this image.
    origin: Literal["seller", "generated", "rendered"] = "seller"
    parent_image_id: UUID | None = None
    fixes: list[Literal["fix", "white_background"]] = Field(default_factory=list)
    role: ImageRole | None = Field(default=None, description="SI12: Back or Close-up.")
    view_url: str | None = Field(
        default=None,
        description=(
            "A signed GET of the photo's server-made copy (long side at most 1024 px, no "
            "metadata), valid about 10 minutes; null until the photo has been checked. A bearer "
            "link: never log or store it. Read the product again before `view_expires_at`."
        ),
    )
    view_expires_at: AwareDatetime | None = Field(
        default=None, description="When to fetch a fresh `view_url`; null with it."
    )


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


class PhotoReadOut(WireModel):
    """SI9: the newest photo read of the product (the generation that saved a version).
    `read` with counts, `unchanged` (same photos as last time), `failed` (the photo call failed,
    the listing was still saved) or `not_read` (no photo, or nothing left to read)."""

    status: Literal["read", "unchanged", "failed", "not_read"]
    at: AwareDatetime
    written: int = Field(ge=0, le=16)
    asked: int = Field(ge=0, le=16)


FieldSourceKind = Literal["sheet", "you", "photo", "confirmed", "size_chart", "brand"]


class FieldSourceOut(WireModel):
    """SI12: where a field's value came from. `sheet` the CSV import, `you` the seller in the app,
    `photo` read from a photo (unconfirmed), `confirmed` a photo value the seller confirmed,
    `size_chart` the seller's chart saved from a size-chart photo, `brand` the brand default."""

    kind: FieldSourceKind
    image_id: UUID | None = None
    confidence: float | None = None


class HsnSuggestionOut(WireModel):
    """SI12: a suggested HSN heading from the category; never stored until the seller saves it."""

    value: str
    basis: Literal["category"]


CategorySuggestionStatus = Literal["suggested", "unavailable", "failed", "confirmed", "overridden"]


class CategoryCandidateOut(WireModel):
    category: Slug
    confidence: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
    rationale: Annotated[str, Field(max_length=300)]


class CategorySuggestionOut(WireModel):
    """SI14: the classifier's suggestion (at most 3 candidates, highest confidence first). It is
    never the product's category by itself: the seller picks one with PATCH (`confirmed` when it
    is a candidate, `overridden` otherwise)."""

    status: CategorySuggestionStatus
    candidates: Annotated[list[CategoryCandidateOut], Field(max_length=3)] = Field(
        default_factory=list
    )
    calibration_version: str | None = None
    at: AwareDatetime | None = None


class SuggestionsOut(WireModel):
    hsn_code: HsnSuggestionOut | None = None
    category: CategorySuggestionOut | None = Field(
        default=None, description="SI14: the stored category suggestion; null on lists."
    )


SizeChartReadStatus = Literal["queued", "reading", "read", "unreadable", "failed", "unavailable"]


class SizeChartSuggestionOut(WireModel):
    """SI12: the chart read from the live size-chart photo, a suggestion the seller edits and
    saves (with `size_chart_image_id`); never a fact by itself."""

    image_id: UUID
    status: SizeChartReadStatus
    chart: SuggestedSizeChart | None = None
    unit_source_line: Annotated[str, Field(max_length=MAX_SIZE_CHART_UNIT_LINE_CHARS)] | None = (
        Field(
            default=None,
            description=(
                "The printed line the read unit came from (for example `All measurements in cm`), "
                "shown so the seller can confirm the unit; null when `chart.unit` is `unknown`."
            ),
        )
    )
    confidence: float | None = None
    read_at: AwareDatetime | None = None


class ProductOut(WireModel):
    id: UUID
    sku: Sku
    title: ProductTitle
    status: Literal["draft", "active"]
    category: Slug | None = Field(description="SI14: null until the seller sets a category.")
    attributes: dict[str, AttributeScalar] = Field(default_factory=dict)
    images: list[StorageKey] = Field(default_factory=list)
    parent_product_id: UUID | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    style_number: str | None = None
    brand_name: str | None = Field(
        default=None,
        description=(
            "SI8: on single reads the effective brand (own, style, else the brand default); "
            "on lists the product's own value."
        ),
    )
    brand_name_inherited: bool = Field(
        default=False,
        description=(
            "SI8: true when neither the product nor its style holds a brand and the brand "
            "default supplied it. Meaningful on single reads only (false on lists)."
        ),
    )
    gender: Gender | None = Field(
        default=None,
        description="SI8: the audience; own or the style's on single reads, own on lists.",
    )
    variant_attributes: dict[str, str] = Field(
        default_factory=dict,
        description="SI8: a variant's axes (`color`, `size`); `{}` on styles and standalones.",
    )
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
    variation_theme: Literal["size", "color", "size_color"] | None = Field(
        default=None,
        description="V1.1: a style's axes; null on a standalone product or a variant.",
    )
    size_run: list[str] = Field(
        default_factory=list,
        description="V1.1: a style's size keys in order (`POST .../size-run` adds to it).",
    )
    photo_facts: list[PhotoFactOut] = Field(default_factory=list)
    photo_read: PhotoReadOut | None = Field(
        default=None, description="The newest photo read; product detail only, null on lists."
    )
    photo_reading: bool = Field(
        default=False,
        description=(
            "V1.1: a photo read on upload is queued or running (product detail only); poll the "
            "product until it is false, then `photo_read` and `photo_facts` show the result."
        ),
    )
    product_identifier: ProductIdentifierOut | None = Field(
        default=None, description="SI5: the barcode (always null on a style)."
    )
    identifier_exemption: bool = Field(
        default=False,
        description="SI5: GTIN-exempt. On a style, true only when every live variant is exempt.",
    )
    hsn_code: str | None = Field(
        default=None, description="SI12: own, else the style's on single reads; own on lists."
    )
    package: PackageOut | None = Field(
        default=None, description="SI12: own, else the style's on single reads; own on lists."
    )
    country_of_origin: str | None = Field(
        default=None,
        description=(
            "SI12: own, else the style's, else the brand default on single reads; own on lists."
        ),
    )
    field_sources: dict[str, FieldSourceOut] = Field(
        default_factory=dict,
        description="SI12: per dictionary key, where the value came from; `{}` on lists.",
    )
    suggestions: SuggestionsOut = Field(
        default_factory=SuggestionsOut,
        description="SI12: values to suggest while the field is empty; empty on lists.",
    )
    size_chart_suggestion: SizeChartSuggestionOut | None = Field(
        default=None,
        description="SI12: the size-chart read of a style or standalone; null on lists.",
    )
    size_chart_read_enabled: bool = Field(
        default=False,
        description=(
            "SI12: whether `POST /products/{product_id}/size-chart-read` can read the photo; while "
            "false it answers `unavailable` (hide the button). False on lists."
        ),
    )
    category_suggest_enabled: bool = Field(
        default=False,
        description=(
            "SI14: whether `POST /products/{product_id}/category-suggestion` can classify the "
            "product; while false it answers `unavailable` (hide the button). False on lists."
        ),
    )
    thumbnail_url: str | None = Field(
        default=None,
        description=(
            "A signed GET of the main photo's server-made copy (a variant shows its style's photo "
            "of its colour), valid about 10 minutes; on lists and single reads. Null when there is "
            "no checked main photo. A bearer link: never log or store it."
        ),
    )
    thumbnail_expires_at: AwareDatetime | None = Field(
        default=None, description="When to fetch a fresh `thumbnail_url`; null with it."
    )


class ProductPage(Page[ProductOut]):
    pass
