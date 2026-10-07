"""Product request and response models (owner step S12).

`ProductCreate` is the site body (spec L11313-11320, DATABASE.md:1197). Each `attributes` value is
wrapped server-side as a seller `Fact`; an unknown key is 422 `UNKNOWN_FACT_KEY` (S12). `images` are
storage keys or upload ids, never URLs; they become `images` rows (T62 default).
"""

from __future__ import annotations

from typing import Annotated, Final, Literal
from uuid import UUID

from mkos_schemas.base import NonBlankStr, Slug, StrictModel, WireModel
from pydantic import AfterValidator, AwareDatetime, Field
from pydantic_core import PydanticCustomError

from app.api.pagination import Page
from app.api.v1.schemas.common import StorageKey

MAX_PRODUCT_IMAGES: Final = 50
MAX_PRODUCT_ATTRIBUTES: Final = 200
MAX_ATTRIBUTE_TEXT: Final = 2000


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


class ProductPage(Page[ProductOut]):
    pass
