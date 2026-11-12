"""The brand profile read and update (owner step S16), and the platform admin's tenant list and
trial recovery (SI5, ADR-0029)."""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal
from uuid import UUID

from mkos_schemas.base import NonBlankStr, StrictModel, WireModel
from pydantic import (
    AfterValidator,
    AwareDatetime,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)
from pydantic_core import PydanticCustomError

from app.api.pagination import Page

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


class TenantCapabilitiesOut(WireModel):
    """What this deployment does today, read from its settings (the same switches the services
    check). The dashboard reads these to word its screens truthfully; they never grant access."""

    amazon_publishing_enabled: bool = Field(
        default=True,
        description=(
            "The Amazon publish kill switch (`AMAZON_PUBLISH_ENABLED`). False: nothing is sent "
            "to Amazon (publish, retry, the automatic resend and the dry run are refused); "
            "approving still works and keeps the listing ready. Same value as "
            "`MarketplaceAccountOut.publishing_enabled` on an Amazon account."
        ),
    )
    category_suggest_enabled: bool = Field(
        default=False, description="`CATEGORY_SUGGEST_ENABLED`: the AI category suggestion."
    )
    column_map_ai_enabled: bool = Field(
        default=False, description="`COLUMN_MAP_AI_ENABLED`: the AI column mapping for imports."
    )
    photo_qa_enabled: bool = Field(
        default=False, description="`PHOTO_QA_ENABLED`: the photo QA warnings."
    )
    category_lamps_enabled: bool = Field(
        default=False, description="`CATEGORY_LAMPS_ENABLED`: listings in the lamps category."
    )
    size_chart_read_enabled: bool = Field(
        default=False, description="`SIZE_CHART_READ_ENABLED`: the size-chart read."
    )
    image_edit_enabled: bool = Field(
        default=False, description="`IMAGE_EDIT_ENABLED`: the AI white-background edit."
    )
    tenant_create_enabled: bool = Field(
        default=False,
        description="`TENANT_CREATE_ENABLED`: an owner can add another brand (ADR-0032).",
    )


class TenantOut(WireModel):
    id: UUID
    name: NonBlankStr
    slug: TenantSlug
    tone: str | None = None
    guidelines: list[str] = Field(default_factory=list)
    glossary: dict[str, str] = Field(default_factory=dict)
    status: Literal["trial", "active", "suspended", "offboarded"]
    trial_ends_at: AwareDatetime | None = Field(
        default=None, description="When the trial ends, while the brand is on trial; else null."
    )
    capabilities: TenantCapabilitiesOut = Field(
        default_factory=TenantCapabilitiesOut,
        description="This deployment's feature switches, so screens never promise what is off.",
    )
    owner_self_approval: bool = Field(
        default=False,
        description=(
            "ADR-0031: owners may approve listings they generated or edited (off by default). "
            "Reviewers never approve their own work, and operators and admins never approve. "
            "Every self-approval is recorded in the listing history."
        ),
    )
    auto_fix_enabled: bool = Field(
        default=True,
        description=(
            "Client item 20: Kuinn applies safe listing fixes by itself (on by default). Off: "
            "each fix Kuinn finds waits as a suggested fix until the seller applies it."
        ),
    )


class TenantPatch(StrictModel):
    """A partial update of the brand profile: at least one field. `tone: null` clears the tone;
    `name`, `guidelines`, `glossary` and `owner_self_approval` cannot be null (send `[]` or `{}`
    to clear a list or map). `glossary` maps a term to avoid to the preferred term
    (`{"kurta outfit": "kurta set"}`). `owner_self_approval` (ADR-0031) is changed by an owner
    only (403 `PERMISSION_DENIED` for anyone else) with a code entered in the last 30 minutes
    under `MFA_ENFORCEMENT=all` (403 `MFA_STEP_UP_REQUIRED` or `MFA_ENROLLMENT_REQUIRED`).
    `auto_fix_enabled` (client item 20) follows the same rules."""

    name: ProfileName | None = None
    tone: Tone | None = None
    guidelines: Annotated[list[Guideline], Field(max_length=GUIDELINES_MAX)] | None = None
    glossary: Annotated[dict[Term, Term], Field(max_length=GLOSSARY_MAX)] | None = None
    owner_self_approval: StrictBool | None = Field(
        default=None,
        description="Owners may approve their own listings (owner only, with a recent code).",
    )
    auto_fix_enabled: StrictBool | None = Field(
        default=None,
        description=(
            "Let Kuinn fix safe listing errors automatically (owner only, with a recent code)."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _nulls(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in (
                "name",
                "guidelines",
                "glossary",
                "owner_self_approval",
                "auto_fix_enabled",
            ):
                if key in data and data[key] is None:
                    raise PydanticCustomError("mkos_invalid", "Only tone may be null.")
        return data

    @model_validator(mode="after")
    def _one_change(self) -> TenantPatch:
        if not self.model_fields_set:
            raise PydanticCustomError("mkos_invalid", "Give at least one field.")
        return self


# ---- trial recovery by a platform admin (SI5, ADR-0029 §5) ----

RECOVERY_REASON_MAX: Final = 500
RecoveryReason = Annotated[
    NonBlankStr, Field(max_length=RECOVERY_REASON_MAX), AfterValidator(_no_nul)
]
TenantStatus = Literal["trial", "active", "suspended", "offboarded"]


class TenantAdminOut(WireModel):
    id: UUID
    name: str
    slug: TenantSlug
    status: TenantStatus
    trial_ends_at: AwareDatetime | None = Field(
        default=None, description="Shown for a `trial` tenant; kept as stored after activation."
    )
    created_at: AwareDatetime


class TenantAdminPage(Page[TenantAdminOut]):
    pass


class TenantActivate(StrictModel):
    reason: RecoveryReason


class TenantExtendTrial(StrictModel):
    days: Annotated[StrictInt, Field(ge=1, le=90)]
    reason: RecoveryReason
