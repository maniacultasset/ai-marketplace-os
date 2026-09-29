# Shared contracts

This is the interface between three teams building in parallel: we own Backend + DB, a separate AI team owns LangGraph/agents/prompts, a separate FE team owns the Next.js dashboard. Eight JSON contracts — CanonicalListing, ErrorReport, ImageReport, AgentRunRecord, ValidationResult, the job envelopes, the GenerationResult the AI pipeline hands back, and the per-field ListingResult adapters return — are the only objects all three teams touch, so they get frozen first and versioned hard. All of them live as Pydantic v2 models in `packages/schemas` (the Day-1 monorepo scaffold in spec [12]), which is the single source of truth: an installable Python distribution the AI team imports, and the generator for the JSON Schema + TypeScript types the FE team imports, with a CI gate that fails any PR where generated files are stale. Read the gaps list before you build: the specs define CanonicalListing twice with different shapes, `agent_runs` has no `tenant_id`, and there are three mutually incompatible listing state machines.

> These JSON shapes are the interface between three teams. Changing one is a breaking change for the AI team and the frontend team.


## Checklist

| Item | What to do | Owner | When |
|---|---|---|---|
| Resolve the double-defined CanonicalListing | Spec [35] (05-listing-generation.md) is NESTED: {listing_id, product_id, marketplace, category, status, content:{title,bullets,description}, keywords:{search_terms,backend_keywords}, attributes, image_reports:[], confidence_score, version:1, created_at}. Spec [06] (00-45-day-mvp-build-plan.md) is FLAT: {product_id, marketplace, title, bullets, description, keywords:{...}, attributes, images:[{image_id,slot}], confidence_score, generated_by, schema_version:'1.0'} — no listing_id, no category, no status, no created_at. ADOPT THE NESTED SHAPE plus three fields borrowed from the flat one (images[], generated_by_agent_run_id, schema_version). Freeze as mkos_schemas.listing.CanonicalListing, tag schemas-v1.0.0. | Tech Lead | blocker |
| Split `images` from `image_reports` — do not nest reports in the listing | CanonicalListing carries `images: [{image_id, slot, position}]` (pointers only). ImageReports are a SIBLING payload, not a field of the listing. Spec [35] puts `image_reports: []` inside the canonical listing; spec [06] puts `images: []`. Both are half right. | Backend | blocker |
| Rename `keywords.backend_keywords` → `keywords.hidden_terms` | `backend_keywords` is Amazon seller-central jargon and is the only genuinely marketplace-specific key in the canonical model. Spec [42] states it plainly: 'Backend keywords — Amazon: 250 bytes hidden; Flipkart: Not applicable.' Rename to `hidden_terms` and define it generically as 'non-visible search metadata; adapters drop it when the target marketplace has no equivalent field'. | Tech Lead | blocker |
| Do NOT encode marketplace limits in the shared Pydantic models | CanonicalListing enforces only structural sanity — title non-empty and ≤500 chars, 1–10 bullets, description ≤5000 chars. Amazon's 200-char title cap and Flipkart's 150-char cap live in the `rules` table and are enforced by the deterministic Rules Engine at validate time. | Tech Lead | blocker |
| Scaffold `packages/schemas` as a real installable Python distribution | Not a loose folder of .py files. `packages/schemas/pyproject.toml`, dist name `mkos-schemas`, import name `mkos_schemas`, hatchling backend, single dependency `pydantic>=2.9,<3`. Installed into apps/api and the AI worker with `uv pip install -e ./packages/schemas`. | Backend | blocker |
| One error-code registry file | `mkos_schemas/codes.py` holds `ErrorCode` and `ImageIssueCode` StrEnums, seeded from spec [122] and [49]. Nobody anywhere types an error code as a bare string. Adding a code is a PR against this file. | Backend | blocker |
| Field-path addressing convention for ErrorReport.field | Dotted path with bracket indexing, and it is the SAME vocabulary in ErrorReport, FieldResult (publish) and the AI correction node: `content.title`, `content.bullets[2]`, `content.description`, `keywords.search_terms`, `keywords.hidden_terms`, `attributes.color`, `images[0]`, and the literal `listing` for whole-object errors (CATEGORY_MISMATCH). | Tech Lead | blocker |
| ErrorReport v1.0.0 — three additions to the spec shape | Take spec [121]'s shape verbatim and add: (1) `severity` on each ErrorItem; (2) `api_error_detail` on each ErrorItem; (3) `request_id` + `listing_id` at the top level. `status` becomes an enum passed\|failed\|partial. | Backend | blocker |
| ImageReport v1.0.0 — add `checks[]` and a real code | Spec [48]'s shape plus: `checks: [{check, method: deterministic\|vision, passed}]` proving which checks ran, `code` on each issue drawn from the [49] taxonomy, `observed`/`expected` dicts, `agent_run_id`. `model_used` becomes nullable (deterministic-only reports made no model call). | Backend | blocker |
| agent_runs: add tenant_id and 9 more columns (migration 0003) | Spec [06]'s DDL has: id, product_id, trigger, model_used, tokens_in, tokens_out, cost, duration_ms, status, error_report_json, created_at. Add: tenant_id (NOT NULL), listing_id, listing_version_id, request_id, job_id, agent_name, attempt, prompt_version, input_digest, started_at. Rename cost→cost_usd, widen DECIMAL(10,6)→NUMERIC(12,6). | DB | blocker |
| validation_runs: fix the source enum, add listing_version_id | Spec [06] DDL says `source IN ('rules','vision','marketplace_api')`; spec [94] says 'rules/vision/api'. Neither includes `schema` even though [120] defines a 3-stage pipeline starting with Pydantic schema validation. Final enum: schema\|rules\|vision\|marketplace_api. Also add tenant_id, listing_version_id, attempt, duration_ms, rules_version. | DB | blocker |
| Job envelope + `GET /api/v1/jobs/{job_id}` | `POST /listings/generate` returns 202 with {job_id, listing_id, state, estimated_duration_seconds, poll_url}. Canonical poll endpoint is `GET /api/v1/jobs/{job_id}` returning JobEnvelope. Keep `GET /listings/{id}/status` from spec [12] as an alias returning the identical JobEnvelope for the newest job on that listing. | Backend | blocker |
| Split persisted listing status from pipeline stage | `listings.status` is the coarse persisted value and uses exactly the DDL CHECK list: created\|generating\|validated\|needs_review\|approved\|publishing\|published\|blocked\|failed. A separate, non-persisted `stage` (loading_context, retrieving_brand, generating, validating_rules, correcting, …) rides on JobEnvelope for progress display only. | Tech Lead | blocker |
| GenerationResult — the AI↔Backend boundary object | The AI pipeline exposes ONE async function, `run_listing_graph(ctx: GenerationContext) -> GenerationResult`, and writes nothing to business tables. GenerationResult carries {listing, image_reports, validation, agent_runs, blocks[], attempts_used, terminal_state, error}. Our ARQ worker persists listing_versions, validation_runs and agent_runs from it inside one transaction. | Backend | blocker |
| ListingResult per-field publish contract (freeze now, build Day 29) | {listing_id, marketplace, external_id, status: published\|partially_published\|failed, idempotency_key, fields: [{field, status, error}], error}. `field` uses the same dotted paths as ErrorReport. | Backend | soon |
| TypeScript generation: `make -C packages/schemas all` | Two generators, two purposes. (a) Contracts: Pydantic `model_json_schema()` → committed `jsonschema/*.schema.json` → `json-schema-to-typescript` → `ts/src/contracts.d.ts`. (b) HTTP surface: FastAPI `app.openapi()` → `openapi.json` → `openapi-typescript` → `ts/src/api.d.ts`. Both outputs are committed to git. | Backend | blocker |
| CI drift gate on every PR | `.github/workflows/contracts.yml`: install packages/schemas, run `make -C packages/schemas all`, then `git diff --exit-code -- packages/schemas/jsonschema packages/schemas/ts`. Stale generated types fail the PR. Add golden-fixture validation and an `oasdiff` breaking-change check against the merge base. | DevOps | blocker |
| Golden fixture library shared by all three teams | `packages/schemas/fixtures/*.json`: canonical_listing.valid.json, canonical_listing.minimal.json, error_report.field_too_long.json, error_report.passed.json, image_report.background_off.json, generation_result.partial.json, listing_result.partial_publish.json. Every team's tests load these exact files. | Backend | soon |
| Mock API server for FE before endpoints exist | `npx @stoplight/prism-cli mock packages/schemas/jsonschema/openapi.json --port 4010`. FE points NEXT_PUBLIC_API_URL at it. Add a `make mock` target and document it in ONBOARDING.md. | Backend | soon |
| Cross-team headers: X-Request-Id and X-Schema-Version | Every request/response carries `X-Request-Id` (uuid4, generated at the edge if absent, propagated through contextvars → ARQ job → agent run → adapter call) and `X-Schema-Version` (the mkos-schemas version the caller was built against). Backend logs a warning on a MAJOR mismatch. | Backend | soon |
| Contract change process: RFC + CODEOWNERS + all-three approval | Any change to packages/schemas requires an ADR in docs/adr/NNNN-contract-<name>.md stating bump level and blast radius per team, approval from Backend + AI + FE owners (enforced via CODEOWNERS on packages/schemas/), fixtures updated in the same PR, and regenerated types committed. MAJOR bumps require a named migration date. | Tech Lead | blocker |
| Publish mkos-schemas to Artifact Registry if the AI team is not in our monorepo | If AI works in a separate repo, stand up a GCP Artifact Registry Python repo and an npm repo, publish on every tag of packages/schemas, and have the AI team pin an exact version. Otherwise: one monorepo, CODEOWNERS per directory. | DevOps | soon |
| Define confidence_score before the FE renders it | Field is frozen now: `confidence_score: float \| null`, range 0.0–1.0, advisory only. The composite formula (rule pass rate + attribute completeness + brand-voice similarity + image compliance, per spec [127]) is owned by the AI team and documented in an ADR by Day 21. | AI team | soon |
| ImportReport envelope for CSV bulk (Day 8) | `POST /products/bulk` → 202 JobAccepted{kind: bulk_import}. Terminal result is ImportReport{batch_id, total_rows, imported, failed, rows: [{row_number, sku, status, errors: [ErrorItem]}]}. Row errors reuse ErrorItem verbatim with source='schema' or 'rules_engine'. | Backend | soon |

## Reference implementation


### packages/schemas — directory layout (single source of truth)

```text
packages/schemas/
├── pyproject.toml                 # dist: mkos-schemas   import: mkos_schemas
├── Makefile                       # make all | check | mock
├── src/mkos_schemas/
│   ├── __init__.py                # __schema_version__ = "1.0.0"
│   ├── base.py                    # StrictModel / WireModel
│   ├── enums.py                   # Marketplace, Severity, ErrorSource, JobState,
│   │                              #   ListingStatus, PipelineStage, ImageSlot
│   ├── codes.py                   # ErrorCode + ImageIssueCode  (the ONLY registry)
│   ├── paths.py                   # dotted field-path vocabulary + resolver
│   ├── listing.py                 # CanonicalListing + content/keywords/image blocks
│   ├── errors.py                  # ErrorItem, ErrorReport
│   ├── images.py                  # ImageCheck, ImageIssue, ImageReport
│   ├── agent.py                   # TokenUsage, AgentRunRecord
│   ├── validation.py              # ValidationResult, ValidationSummary
│   ├── jobs.py                    # GenerateListingRequest, JobAccepted, JobEnvelope,
│   │                              #   GenerationContext, GenerationResult
│   ├── publish.py                 # FieldResult, ListingResult
│   └── export.py                  # model_json_schema() dumper
├── fixtures/                      # golden JSON — all 3 teams' tests load these
├── tests/                         # fixtures must validate against models
├── jsonschema/                    # GENERATED, COMMITTED, CI-verified
│   ├── CanonicalListing.schema.json ... ListingResult.schema.json
│   └── openapi.json
└── ts/                            # GENERATED npm package @mkos/schemas
    ├── package.json
    └── src/{contracts.d.ts, api.d.ts, index.ts}
```

### base.py — two base classes, and why

```python
from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """INBOUND HTTP bodies (things a client sends us).
    extra=forbid -> an unknown key is a 422. Catches FE typos on day one."""
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
        protected_namespaces=(),   # we have fields named model_used
    )


class WireModel(BaseModel):
    """CROSS-TEAM payloads (AI -> BE, BE -> FE, adapter -> BE).
    extra=ignore -> a producer can ship an additive MINOR field before every
    consumer has upgraded mkos-schemas. This is what makes additive contract
    changes non-breaking across three teams building in parallel."""
    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        protected_namespaces=(),
        ser_json_timedelta="iso8601",
    )
```

### enums.py — the shared vocabularies

```python
from enum import StrEnum   # py3.11


class Marketplace(StrEnum):
    AMAZON = "amazon"
    FLIPKART = "flipkart"


class Severity(StrEnum):
    ERROR = "error"        # blocks approval; mirrors rules.severity CHECK
    WARNING = "warning"    # reviewer may accept and proceed


class ErrorSource(StrEnum):
    SCHEMA = "schema"
    RULES_ENGINE = "rules_engine"
    VISION_MODEL = "vision_model"
    AI_MODEL = "ai_model"
    AMAZON_ADAPTER = "amazon_adapter"
    FLIPKART_ADAPTER = "flipkart_adapter"
    SYSTEM = "system"


class JobState(StrEnum):
    QUEUED = "queued"; RUNNING = "running"; SUCCEEDED = "succeeded"
    FAILED = "failed"; TIMEOUT = "timeout"; CANCELLED = "cancelled"


class ListingStatus(StrEnum):
    """PERSISTED in listings.status. Matches the DDL CHECK list exactly."""
    CREATED="created"; GENERATING="generating"; VALIDATED="validated"
    NEEDS_REVIEW="needs_review"; APPROVED="approved"; PUBLISHING="publishing"
    PUBLISHED="published"; BLOCKED="blocked"; FAILED="failed"


class PipelineStage(StrEnum):
    """Progress display only. NEVER written to listings.status."""
    LOADING_CONTEXT="loading_context"; RETRIEVING_BRAND="retrieving_brand"
    GENERATING="generating"; MERGING="merging"; VALIDATING_SCHEMA="validating_schema"
    VALIDATING_RULES="validating_rules"; VALIDATING_VISION="validating_vision"
    CORRECTING="correcting"; SCORING="scoring"; AWAITING_APPROVAL="awaiting_approval"


class ImageSlot(StrEnum):
    MAIN="main"; SECONDARY="secondary"; LIFESTYLE="lifestyle"; SIZE_CHART="size_chart"
```

### codes.py — the single error-code registry

```python
from enum import StrEnum


class ErrorCode(StrEnum):
    # --- top-level summary codes -------------------------------------
    VALIDATION_FAILED = "VALIDATION_FAILED"
    GENERATION_FAILED = "GENERATION_FAILED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    IMPORT_FAILED = "IMPORT_FAILED"
    # --- source=schema ----------------------------------------------
    SCHEMA_INVALID = "SCHEMA_INVALID"
    REQUIRED_FIELD_MISSING = "REQUIRED_FIELD_MISSING"
    # --- source=rules_engine  (spec [122]) ---------------------------
    FIELD_TOO_LONG = "FIELD_TOO_LONG"        # NOT TITLE_TOO_LONG: `field` says which
    FIELD_TOO_SHORT = "FIELD_TOO_SHORT"
    BANNED_TERM = "BANNED_TERM"
    MISSING_ATTRIBUTE = "MISSING_ATTRIBUTE"
    CATEGORY_MISMATCH = "CATEGORY_MISMATCH"
    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"  # factual claim absent from product data
    KEYWORD_DUPLICATE = "KEYWORD_DUPLICATE"
    BYTE_BUDGET_EXCEEDED = "BYTE_BUDGET_EXCEEDED"
    # --- source=ai_model  (spec [122]) -------------------------------
    JSON_INVALID = "JSON_INVALID"
    TIMEOUT = "TIMEOUT"
    TOKEN_LIMIT = "TOKEN_LIMIT"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    # --- source=vision_model  (mirrors ImageIssueCode) ---------------
    IMAGE_NON_COMPLIANT = "IMAGE_NON_COMPLIANT"
    # --- source=*_adapter  (spec [122] + build plan [07]) ------------
    API_REJECTED_PARTIAL = "API_REJECTED_PARTIAL"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    SKU_CONFLICT = "SKU_CONFLICT"
    CATEGORY_NOT_MAPPED = "CATEGORY_NOT_MAPPED"
    # --- source=system -----------------------------------------------
    INTERNAL_ERROR = "INTERNAL_ERROR"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"


class ImageIssueCode(StrEnum):
    """Spec [49] taxonomy, verbatim."""
    BACKGROUND_OFF="BACKGROUND_OFF"; RESOLUTION_LOW="RESOLUTION_LOW"
    TEXT_DETECTED="TEXT_DETECTED"; WATERMARK="WATERMARK"
    FILL_RATIO_LOW="FILL_RATIO_LOW"; WRONG_ASPECT="WRONG_ASPECT"
    FORMAT_UNSUPPORTED="FORMAT_UNSUPPORTED"; FILE_TOO_LARGE="FILE_TOO_LARGE"
    CORRUPT="CORRUPT"; LOGO_PROHIBITED="LOGO_PROHIBITED"
```

### listing.py — CanonicalListing blocks (one block per parallel agent)

```python
from typing import Annotated
from pydantic import Field
from .base import WireModel
from .enums import ImageSlot


class ListingContent(WireModel):
    """EXACTLY the Content Agent's output object (spec [22]).
    Bounds here are structural sanity only. Amazon's 200-char title cap and
    Flipkart's 150-char cap are rows in the `rules` table, NOT constraints here.
    """
    title: Annotated[str, Field(min_length=1, max_length=500)]
    bullets: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=1000)]],
        Field(min_length=1, max_length=10),
    ]
    description: Annotated[str, Field(min_length=1, max_length=5000)]


class ListingKeywords(WireModel):
    """EXACTLY the Keyword Agent's output object.
    `hidden_terms` was called `backend_keywords` in the specs - renamed because
    that is Amazon jargon. Adapters drop it for marketplaces with no such field.
    """
    search_terms: Annotated[list[str], Field(max_length=50)] = []
    hidden_terms: Annotated[list[str], Field(max_length=50)] = []


class ImageRef(WireModel):
    """Pointer only. NO url: storage is GCS and the API mints signed URLs at
    the edge, so the canonical listing never carries a credential-bearing link
    and never carries an s3:// path (the specs' AWS leftovers)."""
    image_id: str
    slot: ImageSlot
    position: int = 0


# Attribute values stay primitive: the Attribute Agent emits CANONICAL values
# ("navy blue"), the adapter maps them to Amazon `Color="Navy"` (spec [28]).
AttributeValue = str | int | float | bool | list[str]
```

### listing.py — CanonicalListing root (AUTHORITATIVE v1.0.0)

```python
from datetime import datetime
from uuid import UUID
from pydantic import Field
from .base import WireModel
from .enums import ListingStatus, Marketplace


class CanonicalListing(WireModel):
    """The contract between the Intelligence layer and the Execution layer.
    INVARIANT (spec [221] #5): no Amazon/Flipkart field NAMES may appear here.
    `marketplace` is a VALUE not a key; `category` is our INTERNAL slug, never
    an Amazon browse node or a Flipkart category id (spec [71] maps those
    inside the adapter).
    """
    schema_version: str = "1.0.0"          # version of THIS contract shape
    listing_id: UUID | None = None         # null until the listings row exists
    product_id: UUID
    marketplace: Marketplace
    category: str                          # internal slug, e.g. ethnic_wear_kurta_sets
    status: ListingStatus = ListingStatus.CREATED
    version: int = 1                       # listing_versions.version - NOT schema_version

    content: ListingContent
    keywords: ListingKeywords = ListingKeywords()
    attributes: dict[str, AttributeValue] = {}
    images: list[ImageRef] = []            # pointers; ImageReports travel separately

    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
    generated_by_agent_run_id: UUID | None = None
    created_at: datetime | None = None

    # NO tenant_id: it comes from the JWT and the row, never from a payload.
    # NO image_reports: those live in listing_versions.image_reports_json.
    # NO marketplace_payload / browse_node / fk_category_id: adapter-internal.
```

### paths.py — the field-path vocabulary shared by errors, correction and publish

```python
import re

# The ONLY legal values for ErrorItem.field and FieldResult.field.
# Used by: rules engine -> ErrorReport -> AI correction node -> adapter FieldResult.
FIELD_PATH_PATTERNS = (
    r"^listing$",                      # whole-object error, e.g. CATEGORY_MISMATCH
    r"^content\.title$",
    r"^content\.bullets\[\d+\]$",
    r"^content\.bullets$",             # cardinality error (too few / too many)
    r"^content\.description$",
    r"^keywords\.search_terms(\[\d+\])?$",
    r"^keywords\.hidden_terms(\[\d+\])?$",
    r"^attributes\.[a-z0-9_]+$",       # attributes.color, attributes.material
    r"^attributes$",                   # MISSING_ATTRIBUTE when the key is absent
    r"^images\[\d+\]$",                # index into CanonicalListing.images
)
_COMPILED = [re.compile(p) for p in FIELD_PATH_PATTERNS]


def is_valid_field_path(path: str) -> bool:
    return any(p.match(path) for p in _COMPILED)


def resolve(listing: "CanonicalListing", path: str):
    """Read the current value at `path`. The correction node uses this to build
    a prompt containing ONLY the failed fields (spec [24])."""
    if path == "listing":
        return listing
    node, m = listing, re.match(r"([a-z_]+)(?:\[(\d+)\])?", "")
    for part in path.split("."):
        m = re.match(r"^([a-z_]+)(?:\[(\d+)\])?$", part)
        if not m:
            raise ValueError(f"illegal field path segment: {part!r}")
        key, idx = m.group(1), m.group(2)
        node = node[key] if isinstance(node, dict) else getattr(node, key)
        if idx is not None:
            node = node[int(idx)]
    return node
```

### errors.py — the unified ErrorReport (spec [121] + 3 additions)

```python
from typing import Any, Literal
from pydantic import Field, field_validator
from .base import WireModel
from .codes import ErrorCode
from .enums import ErrorSource, Marketplace, Severity
from .paths import is_valid_field_path


class ErrorItem(WireModel):
    field: str                                  # dotted path; see paths.py
    error_code: ErrorCode
    severity: Severity = Severity.ERROR         # ADDED: rules.severity had nowhere to go
    message: str                                # human-readable; rendered by FE
    value_submitted: str | None = Field(default=None, max_length=600)
    suggestion: str | None = None               # fed VERBATIM to the correction agent
    source: ErrorSource
    retryable: bool
    marketplace: Marketplace | None = None
    api_error_detail: dict[str, Any] | None = None   # ADDED: raw marketplace payload

    @field_validator("field")
    @classmethod
    def _path(cls, v: str) -> str:
        if not is_valid_field_path(v):
            raise ValueError(f"{v!r} is not a legal field path (see paths.py)")
        return v


class ErrorReport(WireModel):
    schema_version: str = "1.0.0"
    status: Literal["passed", "failed", "partial"]
    error_code: ErrorCode | None = None         # top-level summary; null when passed
    retryable: bool = False
    attempts_used: int = 0
    max_attempts: int = 3                       # spec [25]: bounded at 3
    errors: list[ErrorItem] = []                # ONE array; severity discriminates
    request_id: str | None = None               # ADDED: correlation chain spec [177]
    listing_id: str | None = None               # ADDED

    @property
    def blocking(self) -> list[ErrorItem]:
        return [e for e in self.errors if e.severity is Severity.ERROR]

    @property
    def passed(self) -> bool:
        return not self.blocking
```

### images.py — ImageReport (spec [48] + checks[] + real codes)

```python
from datetime import datetime
from typing import Any, Literal
from uuid import UUID
from pydantic import Field
from .base import WireModel
from .codes import ImageIssueCode
from .enums import ImageSlot, Marketplace, Severity

CheckName = Literal["format", "corruption", "resolution", "aspect_ratio", "file_size",
                    "exif_orientation", "background", "fill_ratio", "text_overlay",
                    "watermark", "logo", "product_visibility"]


class ImageCheck(WireModel):
    """Proves a check RAN. Without this you cannot distinguish 'watermark check
    passed' from 'vision call timed out so we never looked'."""
    check: CheckName
    method: Literal["deterministic", "vision"]
    passed: bool


class ImageIssue(WireModel):
    code: ImageIssueCode            # spec [49] taxonomy; replaces the loose "type"
    check: CheckName
    severity: Severity
    detail: str                     # "Background is RGB(248,250,249), not pure white"
    suggestion: str                 # "Replace background with RGB(255,255,255)"
    observed: dict[str, Any] | None = None   # {"rgb": [248,250,249]} / {"w":800,"h":800}
    expected: dict[str, Any] | None = None   # {"rgb": [255,255,255]} / {"w":1000,"h":1000}


class ImageReport(WireModel):
    schema_version: str = "1.0.0"
    image_id: UUID
    marketplace: Marketplace
    slot: ImageSlot
    pass_: bool = Field(alias="pass")            # "pass" is a Python keyword
    checks: list[ImageCheck] = []
    issues: list[ImageIssue] = []
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    model_used: str | None = None                # null => deterministic-only report
    agent_run_id: UUID | None = None
    processed_at: datetime
```

### agent.py — AgentRunRecord, the telemetry row for EVERY AI call

```python
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID
from .base import WireModel
from .errors import ErrorReport

AgentName = Literal["content", "keyword", "attribute", "image", "correction",
                    "embedding", "quality"]
Trigger = Literal["generate", "regenerate", "correct", "embed", "vision", "bulk_import"]


class TokenUsage(WireModel):
    input: int = 0
    output: int = 0
    cached_input: int = 0      # prompt-cache hits: real money, and spec [149] wants caching


class AgentRunRecord(WireModel):
    """One row per model call. Written ONLY by the backend, from GenerationResult."""
    schema_version: str = "1.0.0"
    id: UUID
    tenant_id: UUID                      # ADDED: absent from spec DDL, breaks cost-per-tenant
    product_id: UUID | None = None       # null for embedding runs
    listing_id: UUID | None = None       # ADDED
    listing_version_id: UUID | None = None
    request_id: str                      # ADDED: correlation chain, spec [177]
    job_id: str | None = None
    agent_name: AgentName                # ADDED: spec [172] wants cost-per-agent
    trigger: Trigger
    attempt: int = 1                     # ADDED: 1..max_attempts, makes retries countable
    model_used: str | None = None        # EXACT provider id, e.g. gpt-4o-mini-2024-07-18
    prompt_version: str | None = None    # spec [150] is phase2 but the column is free now
    tokens: TokenUsage = TokenUsage()
    cost_usd: Decimal = Decimal("0")     # NUMERIC(12,6). Store USD, derive INR in the UI.
    duration_ms: int = 0
    status: Literal["running", "completed", "failed", "timeout"]
    error_report: ErrorReport | None = None
    input_digest: str | None = None      # sha256 of assembled context -> spec [139] re-runs
    started_at: datetime
    created_at: datetime
```

### validation.py — ValidationResult and ValidationSummary

```python
from datetime import datetime
from typing import Literal
from uuid import UUID
from .base import WireModel
from .enums import Marketplace
from .errors import ErrorReport

# Spec DDL says ('rules','vision','marketplace_api'); spec [94] says 'api';
# neither includes 'schema' although spec [120] defines a 3-stage pipeline.
# AUTHORITATIVE:
ValidationSource = Literal["schema", "rules", "vision", "marketplace_api"]


class ValidationResult(WireModel):
    """One row in validation_runs. One per STAGE, not one per validate call."""
    schema_version: str = "1.0.0"
    validation_run_id: UUID | None = None
    tenant_id: UUID
    listing_id: UUID
    listing_version_id: UUID | None = None   # ADDED: validation is always OF a version
    marketplace: Marketplace
    source: ValidationSource
    passed: bool                             # == report has no severity=error item
    attempt: int = 1
    rules_version: int | None = None         # ADDED: pins which rules snapshot judged this
    duration_ms: int = 0
    report: ErrorReport                      # ALWAYS the same ErrorReport shape
    created_at: datetime


class ValidationSummary(WireModel):
    """Body of POST /api/v1/listings/validate. Also embedded in GenerationResult."""
    schema_version: str = "1.0.0"
    listing_id: UUID
    listing_version_id: UUID | None = None
    passed: bool
    stages: list[ValidationResult]           # in execution order: schema, rules, vision
    blocking_count: int = 0
    warning_count: int = 0
    request_id: str | None = None
```

### jobs.py — request / 202 / poll envelopes

```python
from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import Field
from .base import StrictModel, WireModel
from .enums import JobState, Marketplace, PipelineStage
from .errors import ErrorReport

JobKind = Literal["listing_generate", "listing_publish", "bulk_import",
                  "image_compliance", "brand_embed"]


class GenerateListingRequest(StrictModel):     # POST /api/v1/listings/generate
    product_id: UUID
    marketplace: Marketplace
    category: str | None = None                # defaults to the product's mapped category
    user_instructions: str | None = Field(default=None, max_length=2000)
    force_regenerate: bool = False
    # NO tenant_id. It is read from the JWT (spec [156]); a client-supplied
    # tenant_id is a 422 because extra="forbid".


class JobAccepted(WireModel):                  # HTTP 202
    job_id: str
    listing_id: UUID | None = None             # null for bulk_import
    state: JobState = JobState.QUEUED
    estimated_duration_seconds: int = 30
    poll_url: str                              # "/api/v1/jobs/{job_id}"


class JobEnvelope(WireModel):                  # GET /api/v1/jobs/{job_id}
    schema_version: str = "1.0.0"              # and GET /api/v1/listings/{id}/status
    job_id: str
    kind: JobKind
    state: JobState                            # queued|running|succeeded|failed|timeout
    stage: PipelineStage | None = None         # progress only; NOT listings.status
    progress_pct: int = 0
    attempt: int = 0
    max_attempts: int = 3
    listing_id: UUID | None = None
    result_url: str | None = None              # "/api/v1/listings/{id}" when succeeded
    error: ErrorReport | None = None
    request_id: str
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
```

### jobs.py — GenerationContext / GenerationResult (the AI↔Backend boundary)

```python
from .agent import AgentRunRecord
from .images import ImageReport
from .listing import CanonicalListing
from .validation import ValidationSummary


class GenerationContext(WireModel):
    """Everything the AI pipeline gets. Assembled by US, never fetched by them.
    Product text is UNTRUSTED data (Day-17 prompt-injection boundary review)."""
    schema_version: str = "1.0.0"
    request_id: str
    job_id: str
    tenant_id: UUID
    product_id: UUID
    listing_id: UUID
    marketplace: Marketplace
    category: str
    product: dict                    # sku, title, attributes, image_ids
    brand_profile: dict              # tone, guidelines, glossary
    marketplace_rules: list[dict]    # rows from `rules` for this marketplace+category
    category_template: dict          # required / optional attributes
    previous_version: CanonicalListing | None = None
    user_instructions: str | None = None
    max_attempts: int = 3


class AgentBlockStatus(WireModel):
    agent: Literal["content", "keyword", "attribute", "image"]
    status: Literal["ok", "failed", "skipped"]
    agent_run_id: UUID | None = None
    error: ErrorReport | None = None


class GenerationResult(WireModel):
    """The ONLY object `run_listing_graph()` returns. The AI pipeline writes NO
    business tables; the backend persists everything here in one transaction."""
    schema_version: str = "1.0.0"
    request_id: str
    job_id: str
    listing: CanonicalListing | None = None      # null only if every agent failed
    image_reports: list[ImageReport] = []        # -> listing_versions.image_reports_json
    validation: ValidationSummary | None = None
    agent_runs: list[AgentRunRecord] = []        # -> agent_runs rows
    blocks: list[AgentBlockStatus] = []          # partial success, e.g. keyword failed
    attempts_used: int = 0
    terminal_state: Literal["generated", "needs_review", "blocked", "failed"]
    error: ErrorReport | None = None
```

### publish.py — ListingResult (per-field, never a boolean)

```python
from typing import Literal
from uuid import UUID
from .base import WireModel
from .enums import Marketplace
from .errors import ErrorItem, ErrorReport


class FieldResult(WireModel):
    field: str                                     # SAME dotted paths as ErrorItem.field
    status: Literal["succeeded", "failed", "skipped"]
    error: ErrorItem | None = None


class ListingResult(WireModel):
    """Return value of every MarketplaceAdapter method (spec [66]).
    Spec invariant [137]: on partial success retry ONLY the failed fields, so
    this must carry per-field outcomes, never a bare boolean."""
    schema_version: str = "1.0.0"
    listing_id: UUID
    marketplace: Marketplace
    external_id: str | None = None                 # the marketplace's own listing id
    status: Literal["published", "partially_published", "failed"]
    idempotency_key: str                           # spec [136]: every mutation carries one
    fields: list[FieldResult]
    raw_response_digest: str | None = None         # sha256; full body goes to GCS, not the DB
    error: ErrorReport | None = None
    attempt: int = 1
```

### export.py — Pydantic v2 → JSON Schema

```python
"""python -m mkos_schemas.export --out jsonschema/"""
import argparse, json, pathlib
from . import agent, errors, images, jobs, listing, publish, validation

MODELS = [
    listing.CanonicalListing,
    errors.ErrorReport, errors.ErrorItem,
    images.ImageReport,
    agent.AgentRunRecord,
    validation.ValidationResult, validation.ValidationSummary,
    jobs.GenerateListingRequest, jobs.JobAccepted, jobs.JobEnvelope,
    jobs.GenerationContext, jobs.GenerationResult,
    publish.ListingResult,
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    out = pathlib.Path(ap.parse_args().out)
    out.mkdir(parents=True, exist_ok=True)
    for m in MODELS:
        # mode="serialization" + by_alias(default) so ImageReport emits "pass",
        # which is what actually goes on the wire to the FE.
        s = m.model_json_schema(mode="serialization", ref_template="#/$defs/{model}")
        s["$id"] = f"https://schemas.mkos.internal/{m.__name__}.schema.json"
        s["title"] = m.__name__
        (out / f"{m.__name__}.schema.json").write_text(
            json.dumps(s, indent=2, sort_keys=True) + "\n"
        )


if __name__ == "__main__":
    main()
```

### Makefile + packaging + FE wiring

```makefile
# packages/schemas/Makefile
.PHONY: json ts openapi all check mock

json:
	uv run python -m mkos_schemas.export --out jsonschema/

ts: json
	npx --yes json-schema-to-typescript@15 \
	  --input 'jsonschema/*.schema.json' --output ts/src/contracts.d.ts \
	  --additionalProperties false --unreachableDefinitions \
	  --bannerComment '// GENERATED by `make -C packages/schemas all`. DO NOT EDIT.'

openapi:
	cd ../../apps/api && uv run python -c "import json; from app.main import app; \
	  print(json.dumps(app.openapi(), indent=2, sort_keys=True))" \
	  > ../../packages/schemas/jsonschema/openapi.json
	npx --yes openapi-typescript@7 jsonschema/openapi.json -o ts/src/api.d.ts

all: ts openapi

check: all
	@git diff --exit-code -- jsonschema ts \
	  || (echo "STALE GENERATED TYPES: run 'make -C packages/schemas all' and commit"; exit 1)
	uv run pytest tests -q

mock:
	npx --yes @stoplight/prism-cli@5 mock jsonschema/openapi.json --port 4010

# packages/schemas/pyproject.toml
#   [project] name="mkos-schemas" version="1.0.0" requires-python=">=3.11"
#             dependencies=["pydantic>=2.9,<3"]
#   [build-system] requires=["hatchling"] build-backend="hatchling.build"
#   [tool.hatch.build.targets.wheel] packages=["src/mkos_schemas"]
#
# packages/schemas/ts/package.json -> { "name": "@mkos/schemas", "version": "1.0.0",
#                                       "types": "src/index.ts", "files": ["src"] }
# apps/web/package.json            -> "@mkos/schemas": "workspace:*"   (pnpm workspace)
# apps/api + apps/ai               -> uv pip install -e ../../packages/schemas
```

### CI drift gate — .github/workflows/contracts.yml

```yaml
name: contracts
on:
  pull_request:
    paths: ['packages/schemas/**', 'apps/api/**']
jobs:
  schemas:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - uses: actions/setup-node@v4
        with: { node-version: '20' }
      - run: pipx install uv
      - run: uv pip install --system -e ./packages/schemas -e ./apps/api

      - name: Regenerate JSON Schema + TypeScript
        run: make -C packages/schemas all

      - name: Fail on stale generated types
        run: |
          git diff --exit-code -- packages/schemas/jsonschema packages/schemas/ts \
            || { echo '::error::Generated contracts are stale. Run: make -C packages/schemas all'; exit 1; }

      - name: Golden fixtures must validate against the models
        run: uv run --directory packages/schemas pytest tests -q

      - name: Breaking-change check on the HTTP surface
        run: |
          git show origin/${{ github.base_ref }}:packages/schemas/jsonschema/openapi.json > /tmp/base.json || exit 0
          docker run --rm -v /tmp:/t -v $PWD:/w tufin/oasdiff breaking \
            /t/base.json /w/packages/schemas/jsonschema/openapi.json --fail-on ERR

# Mark `contracts` as a REQUIRED status check in branch protection on main,
# alongside ruff + black + mypy (api) and eslint + tsc (web).
```

### fixtures/canonical_listing.valid.json — the golden object

```json
{
  "schema_version": "1.0.0",
  "listing_id": "9c1d2f6a-3b84-4f21-9a55-0c6f8b2e77d1",
  "product_id": "4e8a1b30-77cc-41d9-8f02-5b9c1de3a004",
  "marketplace": "amazon",
  "category": "ethnic_wear_kurta_sets",
  "status": "needs_review",
  "version": 1,
  "content": {
    "title": "Fabrica Organic Cotton Kurta Set for Women | Indigo | Handloom Weave",
    "bullets": [
      "Crafted from 100% organic cotton certified by GOTS for breathable comfort",
      "Traditional handloom weave with a contemporary relaxed silhouette",
      "Set includes full-length kurta and matching palazzo with elasticated waist",
      "Machine washable at 30 degrees C - retains colour wash after wash",
      "Available in sizes XS through XXL with a detailed size guide"
    ],
    "description": "Elevate your ethnic wardrobe with the Fabrica Organic Cotton Kurta Set..."
  },
  "keywords": {
    "search_terms": ["organic cotton kurta", "ethnic wear women", "block print suit"],
    "hidden_terms": ["cotton kurta set", "indigo kurta", "artisan ethnic"]
  },
  "attributes": {
    "color": "navy blue",
    "material": "organic cotton",
    "size": "M",
    "fit": "relaxed",
    "sleeve_length": "three_quarter"
  },
  "images": [
    { "image_id": "1f0b7c22-9d41-4a3e-b0f5-6c2a11e8d900", "slot": "main", "position": 0 },
    { "image_id": "2a9e5d61-4c72-4b88-91aa-7d3f20c4e511", "slot": "secondary", "position": 1 }
  ],
  "confidence_score": 0.91,
  "generated_by_agent_run_id": "7b3c9e10-22d5-4f6a-8c31-9e0a4d5b6f22",
  "created_at": "2026-09-22T10:30:00Z"
}
```

### Alembic 0003_contract_alignment — DDL deltas the contracts require

```sql
-- agent_runs: the spec DDL cannot satisfy spec [172] (cost per tenant / per agent)
ALTER TABLE agent_runs
  ADD COLUMN tenant_id          UUID REFERENCES tenants(id),
  ADD COLUMN listing_id         UUID REFERENCES listings(id),
  ADD COLUMN listing_version_id UUID REFERENCES listing_versions(id),
  ADD COLUMN request_id         TEXT,
  ADD COLUMN job_id             TEXT,
  ADD COLUMN agent_name         TEXT,
  ADD COLUMN attempt            INT NOT NULL DEFAULT 1,
  ADD COLUMN prompt_version     TEXT,
  ADD COLUMN input_digest       TEXT,
  ADD COLUMN started_at         TIMESTAMPTZ;

ALTER TABLE agent_runs ALTER COLUMN cost TYPE NUMERIC(12,6);  -- was DECIMAL(10,6)
ALTER TABLE agent_runs RENAME COLUMN cost TO cost_usd;
-- run the backfill, then:
ALTER TABLE agent_runs ALTER COLUMN tenant_id SET NOT NULL;

CREATE INDEX agent_runs_tenant_created_idx ON agent_runs (tenant_id, created_at DESC);
CREATE INDEX agent_runs_request_idx        ON agent_runs (request_id);
CREATE INDEX agent_runs_listing_idx        ON agent_runs (listing_id);

-- validation_runs: source enum was missing 'schema'; version linkage was missing
ALTER TABLE validation_runs
  ADD COLUMN tenant_id          UUID REFERENCES tenants(id),
  ADD COLUMN listing_version_id UUID REFERENCES listing_versions(id),
  ADD COLUMN attempt            INT NOT NULL DEFAULT 1,
  ADD COLUMN duration_ms        INT,
  ADD COLUMN rules_version      INT;

ALTER TABLE validation_runs DROP CONSTRAINT IF EXISTS validation_runs_source_check;
ALTER TABLE validation_runs ADD CONSTRAINT validation_runs_source_check
  CHECK (source IN ('schema','rules','vision','marketplace_api'));

CREATE INDEX validation_runs_version_idx ON validation_runs (listing_version_id);

-- listings: make the status vocabulary a real constraint, not a comment
ALTER TABLE listings ADD CONSTRAINT listings_status_check CHECK (status IN
  ('created','generating','validated','needs_review','approved',
   'publishing','published','blocked','failed'));
```

### Contract-change RFC template + CODEOWNERS

```text
# docs/adr/NNNN-contract-<name>.md   (one per contract change, no exceptions)
Status:   proposed | accepted | superseded
Contract: CanonicalListing | ErrorReport | ImageReport | AgentRunRecord |
          ValidationResult | JobEnvelope | GenerationResult | ListingResult
Bump:     patch (docs only) | minor (additive OPTIONAL field w/ default) | major (breaking)

## Change            <the exact field diff>
## Why               <what breaks today without it>
## Blast radius
  Backend:  <endpoints / repositories / worker tasks>
  AI team:  <graph nodes / prompt output schemas / merge_outputs>
  FE team:  <components consuming the generated type>
## Migration
  [ ] Old field kept and marked `deprecated` until <date>  (>= +14 days for minor)
  [ ] fixtures/ updated in THIS pr
  [ ] `make -C packages/schemas all` run, generated files committed
  [ ] mkos-schemas version bumped in pyproject.toml AND ts/package.json
## Approvals (all three required before merge)
  [ ] Backend   [ ] AI   [ ] FE

# .github/CODEOWNERS
/packages/schemas/            @tech-lead @backend-lead @ai-lead @fe-lead
/packages/schemas/jsonschema/ @tech-lead
/packages/schemas/ts/         @tech-lead
/apps/api/                    @backend-lead
/apps/ai/                     @ai-lead
/apps/web/                    @fe-lead

# Sprint rules
#  MINOR (additive, optional, defaulted): merge any day. extra="ignore" on
#    WireModel means a consumer on an older version simply drops the new key.
#  MAJOR (rename/remove/retype/newly-required): only in the milestone windows
#    Day 14 (Sun 4 Oct), Day 28 (Sun 18 Oct), Day 42 (Sun 1 Nov) - the plan
#    already has a scope-freeze retro on each of those days.
#  Weekly 30-min contract sync, Thursdays, one engineer per team. Agenda is
#    the open RFCs in docs/adr and nothing else.
```

## Open gaps

- CANONICAL LISTING DEFINED TWICE. Spec [35] (05-listing-generation.md) is nested with `content{}` and `image_reports[]`; spec [06] (00-45-day-mvp-build-plan.md) is flat with top-level title/bullets/description and `images[]`. They also disagree on `version: 1` (int) vs `schema_version: "1.0"` (string) — two different concepts sharing one slot. A third partial shape exists in AgentState (spec [16]) as generated_content / generated_keywords / generated_attributes. RECOMMEND: the nested shape, because each nested block is exactly one parallel agent's output object, so merge_outputs is one assignment per agent; keep BOTH `version` (listing_versions.version, int) and `schema_version` (contract version, semver string) as distinct fields; keep `images[]` (pointers) in the listing and move ImageReports to a sibling payload, matching the two separate JSONB columns already in the listing_versions DDL.
- `backend_keywords` IS the invariant violation. Spec [221] #5 says the canonical model is marketplace-independent, but `keywords.backend_keywords` is Amazon seller-central jargon and spec [42] itself marks it 'Not applicable' for Flipkart. RECOMMEND: rename to `keywords.hidden_terms` today (Day 2, zero code written) and define it generically. For clarity: `marketplace: "amazon"` is NOT a violation (a value, not a key) and neither is `category` provided it holds our internal slug and never an Amazon browse node — spec [71] maps those inside the adapter. Also keep the 250-byte Amazon cap out of the model; it is a `rules` row.
- ERROR ITEMS HAVE NO SEVERITY. The `rules` table has `severity TEXT CHECK (severity IN ('error','warning'))` and ImageReport issues carry severity, but the ErrorReport item shape in spec [121] has none — so the approval gate cannot compute whether a listing is blocked or merely flagged. RECOMMEND: add `severity: error|warning` to ErrorItem, keep ONE `errors` array (splitting warnings out would break the AI correction loop which iterates the array), and expose a `blocking` property.
- `api_error_detail` IS SPECIFIED IN PROSE AND ABSENT FROM THE EXAMPLE. Spec [121] says 'Each error includes: field, error_code, message, value_submitted, suggestion, source, retryable, marketplace, and api_error_detail' but the JSON example omits it. RECOMMEND: include it as `dict | None`, populated only by adapters, holding the raw marketplace payload — spec [66] requires 'the marketplace's raw error string' on partial failure.
- NO FIELD-PATH CONVENTION. Spec [121] uses bare `"title"`, spec [73] uses `"main_image"`. Without one vocabulary, spec [24] ('do NOT regenerate the entire listing if only one field failed') and spec [137] ('retry only failed fields') are unimplementable. RECOMMEND: dotted paths with bracket indexing — content.title, content.bullets[2], keywords.hidden_terms, attributes.color, images[0], and the literal `listing` — validated by a field_validator so an illegal path fails in CI rather than in the correction loop.
- TITLE_TOO_LONG vs FIELD_TOO_LONG. The example in [121] uses TITLE_TOO_LONG; the taxonomy table in [122] uses FIELD_TOO_LONG. RECOMMEND: FIELD_TOO_LONG only. Per-field codes explode into BULLET_TOO_LONG / DESCRIPTION_TOO_LONG / KEYWORD_TOO_LONG and duplicate information already in `field`.
- agent_runs HAS NO tenant_id. The DDL in spec [06] reaches tenant only via a nullable product_id, yet invariant [221] #6 mandates tenant isolation on every query and spec [172] mandates cost-per-tenant. Embedding runs have no product_id at all, so their cost is untenantable. It also has no agent_name (spec [172] wants cost-per-agent, and `trigger` cannot distinguish content from keyword), no request_id (spec [177]'s correlation chain), no attempt, and no started_at. `cost DECIMAL(10,6)` caps at 9999.999999 and the currency is unstated while spec [153] targets '<₹5 per listing'. RECOMMEND: migration 0003 above; store cost_usd NUMERIC(12,6) and derive INR in the dashboard with a recorded fx rate.
- validation_runs SOURCE ENUM IS WRONG IN BOTH PLACES. DDL says ('rules','vision','marketplace_api'); spec [94] says 'rules/vision/api'; neither has 'schema' although spec [120] defines a 3-stage pipeline whose first stage is Pydantic. It also has no listing_version_id, so once v4 exists you cannot answer 'did v3 pass?'. RECOMMEND: enum schema|rules|vision|marketplace_api, plus listing_version_id, tenant_id, attempt, duration_ms and rules_version (needed because spec [70] lets rules change without deployment, which would otherwise silently invalidate yesterday's audit trail).
- THREE INCOMPATIBLE LISTING STATE MACHINES. Spec [17]: CREATED/CONTEXT_LOADED/GENERATING/VALIDATION_PASSED/CORRECTION_REQUIRED/WAITING_APPROVAL/... Build-plan [07]: QUEUED/LOADING_CONTEXT/SCHEMA_VALIDATING/RULE_VALIDATING/VISION_VALIDATING/NEEDS_APPROVAL/... DDL comment: created|generating|validated|needs_review|approved|publishing|published|blocked|failed. RECOMMEND: the DDL list is authoritative for the persisted `listings.status` because it is the one with a database constraint; the other two become LangGraph node names surfaced as a separate, non-persisted `stage` on JobEnvelope. FE filters on `status`, renders the progress bar from `stage`.
- POLLING IS KEYED TWO DIFFERENT WAYS. Spec [106] returns a `job_id`; the only status endpoint in the spec [12] catalogue is `GET /listings/{id}/status`, keyed by listing id — which cannot poll a bulk import (no listing exists yet) or a publish job. RECOMMEND: `GET /api/v1/jobs/{job_id}` as canonical, `GET /api/v1/listings/{id}/status` kept as an alias returning the identical JobEnvelope for that listing's newest job. One FE polling hook covers all four job kinds.
- NOBODY OWNS THE AI↔BACKEND RETURN SHAPE. No spec defines what the LangGraph run hands back, and the specs are contradictory about who writes agent_runs (Day 16 assigns it to OPS, spec [19] implies the agent writes its own). With three parallel teams that ambiguity means two teams writing the same tables with different tenant scoping. RECOMMEND: the AI team exposes one pure async function `run_listing_graph(ctx: GenerationContext) -> GenerationResult` and writes NO business tables; our ARQ worker persists listing_versions, validation_runs and agent_runs from the returned envelope in one transaction. LangGraph's checkpointer gets its own isolated store (Redis, or a `langgraph_checkpoints` table never joined to business tables).
- NO CONTRACT FOR PARTIAL AGENT SUCCESS. Spec [18] runs Content, Keyword, Attribute and Image in parallel but nothing says what happens when one returns and another fails. RECOMMEND: `GenerationResult.blocks: [{agent, status, agent_run_id, error}]` so the FE can show 'keywords unavailable, retry' instead of failing the whole listing, and the correction loop can re-run one agent.
- confidence_score IS UNDEFINED. Specs [127]/[128] list the components (rule pass rate, attribute completeness, brand-voice similarity, image compliance) with no weights and no range, and it collides conceptually with ImageReport.confidence (a per-image vision-model certainty). RECOMMEND: freeze the field now as float 0.0–1.0 nullable and advisory; the AI team documents the formula in an ADR by Day 21. Enforce spec [127]'s invariant in code — the approve endpoint must not read confidence_score at all in MVP.
- AWS LEFTOVERS IN THE CONTRACT SURFACE. Spec [89] describes product images as 'JSONB array of S3 URLs', spec [52] uses `s3://bucket/{tenant_id}/...`, and spec [154] says secrets live in AWS Secrets Manager — but the build plan is entirely GCP (GCS, Secret Manager, Cloud KMS) and the images DDL already uses a neutral `storage_key`. RECOMMEND: no contract ever carries a storage URL. CanonicalListing.images holds `image_id` only; the API mints a short-lived signed GCS URL at the edge. This also keeps a credential-bearing link out of listing_versions.content_json, which is retained forever for audit.
- extra=forbid vs extra=ignore IS AN UNMADE DECISION WITH REAL CONSEQUENCES. forbid everywhere means any additive field from the AI team 422s the backend until all three teams redeploy in lockstep. RECOMMEND: two base classes — StrictModel (extra=forbid) for inbound HTTP bodies so FE typos fail loudly, WireModel (extra=ignore) for cross-team payloads so a producer can ship an additive MINOR field before consumers upgrade. This is what makes the semver policy actually work.
- PYDANTIC GOTCHAS THAT WILL BITE ON DAY 1. `pass` is a Python keyword, so ImageReport needs `pass_: bool = Field(alias='pass')` plus populate_by_name; `model_used` collides with Pydantic v2's protected `model_` namespace, so every model carrying it needs `protected_namespaces=()`; and `model_json_schema()` must be called with `mode='serialization'` or the emitted JSON Schema (and therefore the FE's TypeScript) will show `pass_` instead of `pass`.
- MONOREPO vs THREE REPOS IS UNRESOLVED BY THE SPECS. Spec [12]'s tree assumes one repo and one team. STRONG RECOMMENDATION: one monorepo — apps/api, apps/web, apps/ai, packages/schemas — with CODEOWNERS boundaries. If the AI team insists on a separate repo, stand up a GCP Artifact Registry Python repo plus an npm repo and publish mkos-schemas on every tag; but be honest that at a 45-day pace a three-place publish-bump-PR loop will not happen reliably and the contract will rot.
- JWT CLAIM SHAPE IS A CROSS-TEAM CONTRACT AND IS UNSPECIFIED. Spec [116]/[157] say 'JWT via NextAuth (self-hosted)' while the Day-6 plan says the FastAPI login issues the JWT — two different signers. RECOMMEND: the backend is the sole issuer (HS256, secret in Secret Manager), NextAuth holds it as an opaque session credential; claims frozen as {sub: user_id, tid: tenant_id, roles: [owner|operator|reviewer|admin], exp, iat, jti} and added to packages/schemas as a `JWTClaims` model so FE and backend agree on the role strings.
