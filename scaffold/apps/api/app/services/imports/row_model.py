"""The CSV contract. Header failures are whole-file 422s raised BEFORE the job
is enqueued; row failures are per-row and never fail the batch."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

REQUIRED = ("sku", "title", "category")
OPTIONAL = ("description", "brand", "mrp_inr", "price_inr", "gtin",
            "attributes", "image_urls", "weight_grams", "status")
HEADER = REQUIRED + OPTIONAL
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")
MAX_ROWS = 5_000


def check_header(columns: list[str]) -> list[str]:
    """Returns file-level errors. Non-empty -> 422 IMPORT_FAILED, nothing enqueued."""
    seen = [c.lstrip("﻿").strip().lower() for c in columns]
    errors = [f"HEADER_MISSING_COLUMN: {c}" for c in REQUIRED if c not in seen]
    errors += [f"HEADER_UNKNOWN_COLUMN: {c}" for c in seen if c not in HEADER]
    errors += [f"HEADER_DUPLICATE_COLUMN: {c}" for c in set(seen) if seen.count(c) > 1]
    return errors


class ProductImportRow(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    sku: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,63}$")]
    title: Annotated[str, Field(min_length=1, max_length=500)]
    category: Annotated[str, Field(pattern=r"^[a-z0-9_]{2,64}$")]
    description: Annotated[str, Field(max_length=5000)] = ""
    brand: Annotated[str, Field(max_length=120)] = ""
    mrp_inr: Decimal | None = None
    price_inr: Decimal | None = None
    gtin: str | None = None
    attributes: dict[str, str] = {}
    image_urls: list[str] = []
    weight_grams: Annotated[int, Field(ge=0, le=2_000_000)] | None = None
    status: Literal["draft", "active"] = "draft"

    @field_validator("title", mode="before")
    @classmethod
    def _flatten(cls, v: Any) -> Any:
        return " ".join(str(v).split()) if isinstance(v, str) else v

    @field_validator("mrp_inr", "price_inr", "weight_grams", "gtin", mode="before")
    @classmethod
    def _blank_is_null(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("mrp_inr", "price_inr", mode="before")
    @classmethod
    def _money(cls, v: Any) -> Any:
        if not isinstance(v, str):
            return v
        try:
            d = Decimal(v.replace("₹", "").replace(",", "").strip())
        except InvalidOperation as exc:
            raise ValueError(f"{v!r} is not a price") from exc
        if d < 0 or d.as_tuple().exponent < -2:
            raise ValueError("price must be >= 0 with at most 2 decimal places")
        return d

    @field_validator("gtin")
    @classmethod
    def _gtin(cls, v: str | None) -> str | None:
        if v is not None and (not v.isdigit() or len(v) not in {8, 12, 13, 14}):
            raise ValueError("gtin must be 8, 12, 13 or 14 digits")
        return v

    @field_validator("attributes", mode="before")
    @classmethod
    def _attrs(cls, v: Any) -> Any:
        if not isinstance(v, str) or not v.strip():
            return v or {}
        if v.lstrip().startswith("{"):
            return {str(k): str(x) for k, x in json.loads(v).items()}
        out: dict[str, str] = {}
        for pair in v.split("|"):
            key, sep, val = pair.partition("=")
            key = key.strip().lower()
            if not sep or not key.replace("_", "").isalnum():
                raise ValueError(f"{pair!r} is not key=value")
            if key in out:
                raise ValueError(f"duplicate attribute key {key!r}")
            out[key] = val.strip()
        if len(out) > 50:
            raise ValueError("at most 50 attributes")
        return out

    @field_validator("image_urls", mode="before")
    @classmethod
    def _images(cls, v: Any) -> Any:
        if not isinstance(v, str) or not v.strip():
            return v or []
        urls = [u.strip() for u in v.split("|") if u.strip()]
        if len(urls) > 9:
            raise ValueError("at most 9 images")
        for u in urls:
            if not u.startswith("https://") or len(u) > 2048:
                raise ValueError(f"{u!r} must be an absolute https URL under 2048 chars")
            if not u.split("?")[0].lower().endswith(IMAGE_EXT):
                raise ValueError(f"{u!r} must end in {', '.join(IMAGE_EXT)}")
        return urls

    @model_validator(mode="after")
    def _price_le_mrp(self) -> "ProductImportRow":
        if self.mrp_inr is not None and self.price_inr is not None and self.price_inr > self.mrp_inr:
            raise ValueError("price_inr must be <= mrp_inr")
        return self
