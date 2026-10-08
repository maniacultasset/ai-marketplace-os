"""The brand profile read and update (owner step S16)."""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal
from uuid import UUID

from mkos_schemas.base import NonBlankStr, StrictModel, WireModel
from pydantic import AfterValidator, Field, model_validator
from pydantic_core import PydanticCustomError

# The tenant slug is `ck_tenants_slug` (0001_core_tenancy; DATABASE.md tenants row): hyphens, no
# underscores, 2-40 chars. It is not the category/rule `Slug` (`^[a-z0-9_]{2,64}$`).
TenantSlug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,39}$")]

NAME_MAX: Final = 200
TONE_MAX: Final = 2000
GUIDELINES_MAX: Final = 100
GUIDELINE_MAX: Final = 500
GLOSSARY_MAX: Final = 500
TERM_MAX: Final = 100


def _no_nul(value: str) -> str:
    if "\x00" in value:
        raise PydanticCustomError("mkos_invalid", "Text must not contain NUL.")
    return value


ProfileName = Annotated[NonBlankStr, Field(max_length=NAME_MAX), AfterValidator(_no_nul)]
Tone = Annotated[str, Field(min_length=1, max_length=TONE_MAX), AfterValidator(_no_nul)]
Guideline = Annotated[NonBlankStr, Field(max_length=GUIDELINE_MAX), AfterValidator(_no_nul)]
Term = Annotated[NonBlankStr, Field(max_length=TERM_MAX), AfterValidator(_no_nul)]


class TenantOut(WireModel):
    id: UUID
    name: NonBlankStr
    slug: TenantSlug
    tone: str | None = None
    guidelines: list[str] = Field(default_factory=list)
    glossary: dict[str, str] = Field(default_factory=dict)
    status: Literal["trial", "active", "suspended", "offboarded"]


class TenantPatch(StrictModel):
    """A partial update of the brand profile: at least one field. `tone: null` clears the tone;
    `name`, `guidelines` and `glossary` cannot be null (send `[]` or `{}` to clear a list or map).
    `glossary` maps a term to avoid to the preferred term (`{"kurta outfit": "kurta set"}`)."""

    name: ProfileName | None = None
    tone: Tone | None = None
    guidelines: Annotated[list[Guideline], Field(max_length=GUIDELINES_MAX)] | None = None
    glossary: Annotated[dict[Term, Term], Field(max_length=GLOSSARY_MAX)] | None = None

    @model_validator(mode="before")
    @classmethod
    def _nulls(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in ("name", "guidelines", "glossary"):
                if key in data and data[key] is None:
                    raise PydanticCustomError("mkos_invalid", "Only tone may be null.")
        return data

    @model_validator(mode="after")
    def _one_change(self) -> TenantPatch:
        if not self.model_fields_set:
            raise PydanticCustomError("mkos_invalid", "Give at least one field.")
        return self
