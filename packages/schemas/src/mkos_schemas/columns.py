"""AI seam payloads for one spreadsheet column mapping (SI14; AI-42).

The backend's exact names, aliases and fuzzy match go first; only the headers they leave unmapped
reach the pipeline, with the importer's accepted header names as the `dictionary` and the names
already taken as `taken_keys`. Sample cells are never sent (`samples` stays empty): seller cells may
hold addresses or names. The answer is a key (or null) and a confidence per request header; the
backend drops a key outside the dictionary or already taken, and a person confirms the map before
any upload uses it.

Field names and caps follow the pipeline's contract 1.1.0 (`mkos_pipeline.contracts`). Records: agent
`attribute`, trigger `generate`, prompt versions starting `map_columns/`.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import AfterValidator, ConfigDict, Field, StringConstraints

from .agent import AgentRunRecord
from .base import SCHEMA_VERSION, RequestId, WireModel
from .classify import DictionaryKey
from .errors import ErrorReport

__all__ = [
    "MAX_DICTIONARY_KEYS",
    "MAX_LEFTOVER_HEADERS",
    "MAX_SAMPLE_CELLS",
    "ColumnMapRequest",
    "ColumnMapResult",
    "ColumnMapping",
    "ColumnSample",
    "DictionaryEntry",
]

MAX_LEFTOVER_HEADERS: Final = 60
MAX_SAMPLE_CELLS: Final = 5
MAX_DICTIONARY_KEYS: Final = 120

_Text200 = Annotated[str, StringConstraints(max_length=200)]


def _printable_header(value: str) -> str:
    if value.strip() == "":
        raise ValueError("a header must not be blank")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("a header must not hold control characters")
    return value


class ColumnSample(WireModel):
    header: Annotated[
        str, StringConstraints(min_length=1, max_length=200), AfterValidator(_printable_header)
    ]
    samples: Annotated[list[_Text200], Field(max_length=MAX_SAMPLE_CELLS)] = Field(
        default_factory=list
    )


class DictionaryEntry(WireModel):
    key: DictionaryKey
    label: _Text200 | None = None
    aliases: Annotated[list[_Text200], Field(max_length=12)] = Field(default_factory=list)
    value_type: Literal["text", "enum", "number", "measure", "boolean", "list"] = "text"
    allowed_values: Annotated[list[_Text200], Field(max_length=40)] | None = None


class ColumnMapRequest(WireModel):
    model_config = WireModel.model_config | ConfigDict(hide_input_in_errors=True)

    schema_version: str = SCHEMA_VERSION
    tenant_id: UUID
    product_id: UUID | None = None
    job_id: UUID | None = None
    request_id: RequestId
    headers: Annotated[list[ColumnSample], Field(min_length=1, max_length=MAX_LEFTOVER_HEADERS)]
    dictionary: Annotated[
        list[DictionaryEntry], Field(min_length=1, max_length=MAX_DICTIONARY_KEYS)
    ]
    taken_keys: Annotated[list[DictionaryKey], Field(max_length=MAX_DICTIONARY_KEYS)] = Field(
        default_factory=list
    )


class ColumnMapping(WireModel):
    key: DictionaryKey | None
    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class ColumnMapResult(WireModel):
    status: Literal["ok", "failed"]
    # Every request header, keyed by the header text: a key (or null) and a confidence.
    mappings: Annotated[dict[str, ColumnMapping], Field(max_length=MAX_LEFTOVER_HEADERS)] = Field(
        default_factory=dict
    )
    confidence_version: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    agent_runs: Annotated[list[AgentRunRecord], Field(max_length=12)] = Field(default_factory=list)
    error: ErrorReport | None = None
