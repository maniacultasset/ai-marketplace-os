"""SI14: spreadsheet column maps (`POST /imports/column-maps`, `GET .../{map_id}`,
`POST .../{map_id}/confirm`).

The seller's header row is mapped onto the importer's accepted header names: exact names and
aliases first, then a fuzzy match, then (switch on) the AI for the leftovers. A person confirms the
map, answering every column that needs review, before `POST /products/bulk` uses it with
`column_map_id`. Raw header text is never echoed: `header_shown` is the header only when it is a
plain name, else `column N`, and confirm answers are keyed by column position.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal
from uuid import UUID

from mkos_schemas.base import StrictModel, WireModel
from pydantic import AfterValidator, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.core.text import has_control_char

MAX_MAP_HEADERS: Final = 200
MAX_HEADER_CHARS: Final = 120
MAX_SAMPLES_PER_HEADER: Final = 3
MAX_SAMPLE_CHARS: Final = 64

ColumnMapStatus = Literal["mapping", "ready", "confirmed", "failed"]
MapMethod = Literal["exact", "alias", "fuzzy", "ai", "none"]


def _header(value: str) -> str:
    if value.strip() == "":
        raise PydanticCustomError("mkos_invalid", "A header must not be blank.")
    if has_control_char(value) or "\n" in value or "\t" in value:
        raise PydanticCustomError("mkos_invalid", "A header must not hold control characters.")
    return value


def _sample(value: str) -> str:
    if has_control_char(value):
        raise PydanticCustomError("mkos_invalid", "A sample must not hold control characters.")
    return value


Header = Annotated[str, Field(min_length=1, max_length=MAX_HEADER_CHARS), AfterValidator(_header)]
SampleCell = Annotated[str, Field(max_length=MAX_SAMPLE_CHARS), AfterValidator(_sample)]
Position = Annotated[int, Field(ge=1, le=MAX_MAP_HEADERS)]


class ColumnMapCreate(StrictModel):
    headers: Annotated[list[Header], Field(min_length=1, max_length=MAX_MAP_HEADERS)]
    samples: Annotated[
        dict[Header, Annotated[list[SampleCell], Field(max_length=MAX_SAMPLES_PER_HEADER)]],
        Field(max_length=MAX_MAP_HEADERS),
    ] = Field(
        default_factory=dict,
        description=(
            "Up to 3 cells per header, kept for your own review only (never sent to a model), "
            "cleared at confirm or after 7 days. Keys must be headers of this map."
        ),
    )

    @model_validator(mode="after")
    def _samples_of_headers(self) -> ColumnMapCreate:
        if not set(self.samples) <= set(self.headers):
            raise PydanticCustomError("mkos_invalid", "A sample names a header not in the map.")
        return self


class ColumnMapEntry(WireModel):
    position: Position
    header_shown: str
    key: str | None = None
    method: MapMethod
    confidence: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
    needs_review: bool


class ColumnMapOut(WireModel):
    id: UUID
    status: ColumnMapStatus
    mappings: Annotated[list[ColumnMapEntry], Field(max_length=MAX_MAP_HEADERS)]
    needs_review_count: Annotated[int, Field(ge=0)]
    created_at: datetime


class ColumnMapAnswer(StrictModel):
    position: Position
    key: Annotated[str, Field(min_length=1, max_length=64)] | None = None


class ColumnMapConfirm(StrictModel):
    mappings: Annotated[list[ColumnMapAnswer], Field(min_length=1, max_length=MAX_MAP_HEADERS)]

    @field_validator("mappings")
    @classmethod
    def _unique_positions(cls, value: list[ColumnMapAnswer]) -> list[ColumnMapAnswer]:
        positions = [answer.position for answer in value]
        if len(set(positions)) != len(positions):
            raise PydanticCustomError("mkos_invalid", "A position appears more than once.")
        return value
