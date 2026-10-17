#!/usr/bin/env python3
"""Generate the handover API reference from the committed contract.

Reads `packages/schemas/jsonschema/openapi.json` (the contract the FE types come from) and writes:

- `docs/handover/API-REFERENCE.md`: conventions, a summary row per operation, then one section
  per operation with its parameters, request body, responses and error codes.
- `docs/handover/swagger.html`: Swagger UI with the spec embedded inline, so it opens from disk.

Two facts the spec does not carry are read from the code with `ast` (no import, no database):
the role x action matrix (`apps/api/app/core/rbac.py`) and each error code's retry class
(`packages/schemas/src/mkos_schemas/codes.py`). The script uses the standard library only, writes
no timestamps and sorts everything, so the same contract always gives the same bytes.

Run it with `make docs-handover`, or `python scripts/docs/gen_api_reference.py` from the repo root.
`--check` regenerates in memory and exits 1 when a committed output differs.
"""

from __future__ import annotations

import argparse
import ast
import copy
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = ROOT / "packages/schemas/jsonschema/openapi.json"
RBAC_PATH = ROOT / "apps/api/app/core/rbac.py"
CODES_PATH = ROOT / "packages/schemas/src/mkos_schemas/codes.py"
ENUMS_PATH = ROOT / "packages/schemas/src/mkos_schemas/enums.py"
OUT_MD = ROOT / "docs/handover/API-REFERENCE.md"
OUT_HTML = ROOT / "docs/handover/swagger.html"

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
METHOD_ORDER = {m: i for i, m in enumerate(HTTP_METHODS)}

BASE_URLS = (
    (
        "Local (your machine, `make dev`)",
        "http://localhost:8000",
        "Port is `API_HOST_PORT` (default 8000).",
    ),
    (
        "Local mock (`make mock`)",
        "http://127.0.0.1:4010",
        "Answers every operation with its example from the contract; send `Prefer: code=<status>` "
        "for a declared error. No database, no real data.",
    ),
    ("Staging", "https://api.staging.<DOMAIN>", "Placeholder: filled in after the GCP rollout."),
    ("Production", "https://api.<DOMAIN>", "Placeholder: filled in after the GCP rollout."),
)

# Swagger UI from jsdelivr, pinned. The SRI hashes were computed locally (openssl dgst -sha384)
# from copies downloaded from cdn.jsdelivr.net and byte-compared with the npm tarball of the same
# version. To bump: download the three files, compare with the tarball, recompute, edit here.
SWAGGER_UI_VERSION = "5.33.1"
SWAGGER_UI_SRI = {
    "swagger-ui.css": "sha384-Ov4/wv3j2bmct8cDc5X4ngJZohVPzEmc6uDPH8WeljUxO5vtoykvMEfbu9Vh6RaW",
    "swagger-ui-bundle.js": "sha384-ZPehFMQommnnuaZ4rpxgkgTT2DKFVp4hZC/7pLit+9Lek9T1YGSo23eHFbvNkXkw",
    "swagger-ui-standalone-preset.js": (
        "sha384-My2aDM4r2Mbm3ybHcubKm9O9U8FEjvF/O5nGvE9YK5dzqOTbWEKa79RPJ1krdMaF"
    ),
}

# Tag order follows a user's journey through the product. A tag the spec gains later and this
# list lacks is appended alphabetically, so nothing is ever dropped.
TAG_ORDER = (
    "auth",
    "tenants",
    "members",
    "brand",
    "products",
    "imports",
    "images",
    "listings",
    "jobs",
    "issues",
    "tasks",
    "marketplaces",
    "rules",
    "analytics",
    "signup",
    "admin",
    "health",
)
TAG_BLURBS = {
    "auth": (
        "Log in, renew tokens, log out, read who you are, and switch the active tenant. "
        "Login, refresh and logout are public (no access token); they are rate limited per IP."
    ),
    "tenants": (
        "The brand profile of the caller's tenant: tone, guidelines and glossary. The AI uses it "
        "when it writes listings. Updates use `ETag` / `If-Match`."
    ),
    "members": "The people in the tenant and their roles. Only owners and admins manage the team.",
    "signup": (
        "Self-serve brand sign-up (ADR-0019): a pending request a platform admin approves, and "
        "the applicant's status read. Both are public and rate limited per IP."
    ),
    "admin": (
        "The platform admin's sign-up queue: list, approve (creates the trial tenant and its "
        "owner) and reject. Platform admins only; everyone else gets 403."
    ),
    "brand": (
        "Brand documents (style guides and similar). An upload is split into chunks and embedded "
        "in the background, so the AI can quote the brand's own words."
    ),
    "products": (
        "Products hold the seller's facts, marketplace-neutral. Create one at a time, or import a "
        "CSV in bulk (a background job)."
    ),
    "imports": "The report and the rows of a CSV import, and a retry of only the failed rows.",
    "images": (
        "Product images. Either upload the file through the API, or get a signed URL, PUT the file "
        "to storage, then register the key. Every image goes through background compliance checks."
    ),
    "listings": (
        "The core flow: generate a listing with AI, validate it, edit it (each edit is a new "
        "version), approve it (maker-checker), publish it to a marketplace, then follow its "
        "marketplace status."
    ),
    "jobs": "Every background job is readable here. Poll a `job_id` from any 202 answer.",
    "issues": "The issue inbox: problems the system found on listings, with a suggested fix.",
    "tasks": "The human task inbox: questions only a person can answer (a missing fact, a reconnect).",
    "marketplaces": (
        "Connect an Amazon or Flipkart seller account with OAuth, list the connected accounts and "
        "revoke one. A marketplace that is not configured answers 501 `NOT_IMPLEMENTED`: Flipkart "
        "is off unless `FLIPKART_ENABLED=true`, and Amazon needs `AMAZON_AUTHORIZE_URL` (`make dev` "
        "points both at a local fake marketplace)."
    ),
    "rules": "The Rules Engine's rules, and a dry run that checks a draft listing without saving.",
    "analytics": "Operational counts for a date range.",
    "health": (
        "Probes for the load balancer and for you. Not under `/api/v1`, no auth, never rate "
        "limited. They answer plain JSON, not the error envelope."
    ),
}

# Extra Markdown shown under a tag's blurb: the flows a newcomer needs to see once.
TAG_EXTRAS = {
    "marketplaces": r"""
How a seller account gets connected:

```mermaid
sequenceDiagram
  participant B as Browser
  participant API as API
  participant M as Marketplace
  B->>API: POST /marketplaces/connect (marketplace)
  API-->>B: 200 authorize_url
  B->>M: open authorize_url, the seller consents
  M-->>B: redirect to the API callback with state and code
  B->>API: GET /marketplaces/amazon/callback (state, spapi_oauth_code)
  API->>M: exchange the code for tokens, stored encrypted
  API-->>B: 303 to DASHBOARD_BASE_URL/marketplaces/connected?pending=handle
  B->>API: POST /marketplaces/connect/confirm (pending)
  API-->>B: 200 the marketplace account, status active
```

How to read it: the browser is redirected through the marketplace and back; the frontend makes only the first and the last call. The callback carries no access token (the marketplace's redirect cannot), so the backend holds the new account as pending and the frontend page at `/marketplaces/connected` must send the `pending` handle to `connect/confirm` with the user's token. The handle and the OAuth `state` are single-use and expire (`OAUTH_STATE_TTL_SECONDS`, default 600 seconds); a replay is 409 `OAUTH_STATE_INVALID`. Flipkart sends `code` instead of `spapi_oauth_code`.

<sub>Source: apps/api/app/api/v1/routers/marketplaces.py, apps/api/app/api/v1/schemas/marketplaces.py (CALLBACK_LOCATION_TEMPLATE), apps/api/app/core/config.py, scripts/e2e/marketplace_connect.sh</sub>
""",
}

JOURNEY = r"""
## A typical journey

The order in which a brand's data moves through the API, from a CSV to a live listing. It is the order the end-to-end acceptance test (`scripts/e2e/acceptance.sh`) follows.

```mermaid
flowchart TD
  conn["POST /marketplaces/connect, then confirm: connect a seller account"]
  imp["POST /products/bulk: import products from a CSV"]
  brand["POST /brand/documents: upload the brand guide"]
  img["POST /images/upload: one call per product image"]
  gen["POST /listings/generate: the AI writes a draft version"]
  get["GET /listings/listing_id: read content and validation"]
  edit["PATCH /listings/listing_id/content with If-Match: a new version"]
  val["POST /listings/validate: re-check against the current rules"]
  appr["POST /listings/approve: a reviewer or owner signs off"]
  prev["GET /listings/listing_id/preview: the payload that will be sent"]
  pub["POST /listings/publish: send it to the marketplace"]
  sub["GET /listings/listing_id/submissions: per-field results"]
  live["POST /listings/listing_id/refresh-status: check marketplace_state"]
  imp --> img
  brand --> gen
  img --> gen
  gen --> get
  get -->|"needs changes"| edit
  edit --> get
  get -->|"looks right"| val
  val -->|"passed"| appr
  appr --> prev
  prev --> pub
  conn --> pub
  pub --> sub
  sub --> live
```

How to read it: each box is one API call, and arrows show what must happen first. Calls that answer `202` (import, brand upload, image upload, generate, publish, refresh-status) start a [background job](#background-work-202-and-job-polling); poll it before you move to the next box. Generation and every content edit already run the Rules Engine and leave the listing `validated` or `needs_review`. `validate` re-checks on demand: an approval needs a passed check against the rules in force now, so if the rules changed since, approve answers 409 `STALE_VERSION` until you validate again. A publish needs an approved listing and a connected account for that marketplace.

<sub>Source: scripts/e2e/acceptance.sh, apps/api/app/services/generation_result.py, apps/api/app/services/listing_edit.py, apps/api/app/services/listing_validate.py, apps/api/app/services/review.py</sub>
"""

# Where the spec's method and headers do not tell the whole retry story, the route's own
# documented behaviour does. Keyed by operationId; a key the spec no longer has fails the run.
IDEMPOTENT_NOTES = {
    "auth_logout": "yes (always 204; a repeat is a no-op)",
    "auth_refresh": "no (a repeat counts as token reuse and ends the session)",
    "brand_documents_delete": "yes (a repeat is 404)",
    "marketplaces_callback": "no (the OAuth state is single-use; a replay is 409)",
    "marketplaces_accounts_revoke": "yes (a repeat returns the account unchanged)",
    "marketplaces_connect_confirm": "no (the pending handle is single-use; a repeat is 409)",
    "members_patch": "yes (a repeat sets the same values again)",
    "products_create": "no (a repeat with the same SKU is 409 `SKU_CONFLICT`)",
    "listings_validate": "yes (a repeat with unchanged rules returns the stored result)",
    "rules_dry_run": "yes (saves nothing)",
}

# Summaries for operations whose spec summary is only the function name.
SUMMARY_OVERRIDES = {
    "healthz_healthz_get": "Liveness probe: always 200, never touches the database",
    "readyz_readyz_get": "Readiness probe: 200 when the database answers, else 503",
}

# Bodies the code returns where the spec declares no schema (apps/api/app/api/health.py).
# Keyed by (operationId, status).
BODY_NOTES = {
    ("healthz_healthz_get", "200"): {"status": "ok"},
    ("readyz_readyz_get", "200"): {"status": "ok", "checks": {"postgres": "ok"}},
}

# Error codes the code returns that the contract's response descriptions do not list yet (the
# contract declares the codes of its generic test cases only). Keyed by operationId:
# [(status, codes, when, source)]. Every code must be in the spec's ErrorCode enum.
_GENERATE_GATE = (
    "409",
    ("LISTING_PAUSED", "LISTING_ARCHIVED", "LISTING_BUSY", "INVALID_STATUS_TRANSITION"),
    "The listing is paused, archived, already generating, or in a status that cannot move.",
    "apps/api/app/services/generation.py (_status_gate)",
)
_BUDGET = (
    "429",
    ("BUDGET_EXCEEDED",),
    "The tenant's daily AI budget is spent; `Retry-After` is the time to the next midnight UTC.",
    "apps/api/app/api/v1/routers/listings.py (_budget), apps/api/app/services/budget.py",
)
CODE_NOTES = {
    "listings_generate": [_GENERATE_GATE, _BUDGET],
    "listings_regenerate": [_GENERATE_GATE, _BUDGET],
}

# Per-item behaviour the response table cannot show. Keyed by operationId.
OP_NOTES = {
    "listings_approve": (
        "> **Note.** Maker-checker is checked per item: a refused item comes back inside the "
        "`200` body with `http_status` 403 and `error.error_code` `MAKER_CHECKER_VIOLATION`, and "
        "the other items still run. The top-level `403` is only `PERMISSION_DENIED` (a role "
        "without `listing:approve`, such as an operator). A reject with `regenerate` can also "
        "fail per item with 429 `BUDGET_EXCEEDED`. Source: `apps/api/app/services/review.py`, "
        "`scripts/e2e/acceptance.sh` (owner_self_approve_item_403)."
    ),
}

# Spec examples that disagree with what the running code returns. Each entry is
# (operationId, status, field, value in the spec, value the code returns, source). The value is
# corrected in both outputs and a note is shown. When the contract is fixed (the spec already holds
# the right value) the entry is skipped; any third value fails the run, so a stale entry is seen.
EXAMPLE_FIXES = (
    (
        "products_bulk",
        "202",
        "poll_url",
        "/api/v1/imports/0b1c2d3e-0000-4000-8000-000000000401",
        "/api/v1/jobs/0b1c2d3e-0000-4000-8000-000000000301",
        "apps/api/app/services/imports/upload.py (accepted_body)",
    ),
)

GLOSSARY = (
    (
        "Tenant",
        "One brand (one seller business). All data belongs to exactly one tenant. Your access token names the active tenant; no route takes a `tenant_id`.",
    ),
    (
        "Member / role",
        "A user's membership in a tenant, with one role: `owner`, `admin`, `operator` or `reviewer`. A user can belong to several tenants.",
    ),
    (
        "Product",
        "The seller's facts about one item (SKU, title, category, attributes, images), independent of any marketplace.",
    ),
    (
        "Family / variant",
        "Apparel is sold as a style (the family, one parent product) with child variant products, one per colour and size. The backend stores the full size x colour matrix, so the AI never invents a variant that does not exist.",
    ),
    (
        "Listing",
        "One product prepared for one marketplace (for example the Amazon listing of SKU-001). It has two states: our lifecycle `status` (@@LISTING_STATUS@@) and the marketplace's own view, `marketplace_state` (@@MARKETPLACE_STATE@@).",
    ),
    (
        "Listing version",
        "An immutable snapshot of a listing's content (a `CanonicalListing`). Generation and every human edit create a new version; `current_version_id` points to the newest.",
    ),
    (
        "Maker-checker",
        "Whoever generated or edited a version (or an earlier version since the last full generation) can never approve it; another active reviewer or owner must. This stops one person from both writing and signing off content. A violation is a 403 `MAKER_CHECKER_VIOLATION` on that item inside the 200 batch answer of `POST /api/v1/listings/approve`.",
    ),
    (
        "Marketplace account",
        "A seller account on Amazon or Flipkart connected with OAuth. The backend stores its tokens encrypted and never returns them.",
    ),
    (
        "Job run",
        "One background job (generate, publish, import, image check, brand embed, status sync). A 202 answer gives its `job_id`; read it with `GET /api/v1/jobs/{job_id}`.",
    ),
    (
        "Issue",
        "A problem found on a listing (a rule failure, a marketplace rejection), with an error code and a suggested fix. Shown in the issue inbox.",
    ),
    (
        "Human task",
        "A question only a person can answer, such as a missing fact or a reconnect. Shown in the task inbox.",
    ),
    (
        "Rules Engine",
        "The deterministic checker. It validates listing content against versioned rules and marketplace templates and answers with an `ErrorReport`.",
    ),
)


# ---------------------------------------------------------------------------------------------
# Facts read from the code with `ast`.


def _enum_classes(path: Path) -> dict[str, dict[str, str]]:
    """{class name: {member name: value}} for every class whose body assigns string constants."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        members: dict[str, str] = {}
        for stmt in node.body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                members[stmt.targets[0].id] = stmt.value.value
        if members:
            out[node.name] = members
    return out


def load_role_matrix() -> dict[str, list[str]]:
    """{action value: sorted roles holding it}, from `MATRIX` in rbac.py."""
    tree = ast.parse(RBAC_PATH.read_text(encoding="utf-8"))
    actions = _enum_classes(RBAC_PATH).get("Action")
    if not actions:
        raise SystemExit(f"{RBAC_PATH}: no Action enum found")
    matrix_value: ast.expr | None = None
    for node in tree.body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        if target == "MATRIX":
            matrix_value = value
    if not isinstance(matrix_value, ast.Call) or not matrix_value.args:
        raise SystemExit(f"{RBAC_PATH}: MATRIX is not MappingProxyType({{...}})")
    table = matrix_value.args[0]
    if not isinstance(table, ast.Dict):
        raise SystemExit(f"{RBAC_PATH}: MATRIX argument is not a dict literal")
    by_action: dict[str, set[str]] = {value: set() for value in actions.values()}
    for key, val in zip(table.keys, table.values, strict=True):
        if not isinstance(key, ast.Constant) or not isinstance(val, ast.Call):
            raise SystemExit(f"{RBAC_PATH}: unexpected MATRIX entry")
        role = str(key.value)
        arg = val.args[0] if val.args else None
        if isinstance(arg, ast.Name) and arg.id == "Action":
            held = set(actions.values())
        elif isinstance(arg, ast.Set):
            held = set()
            for elt in arg.elts:
                if not isinstance(elt, ast.Attribute) or elt.attr not in actions:
                    raise SystemExit(f"{RBAC_PATH}: unexpected action in MATRIX[{role!r}]")
                held.add(actions[elt.attr])
        else:
            raise SystemExit(f"{RBAC_PATH}: unexpected value for MATRIX[{role!r}]")
        for action in held:
            by_action[action].add(role)
    return {action: sorted(roles, key=ROLE_ORDER.index) for action, roles in by_action.items()}


ROLE_ORDER = ["owner", "admin", "operator", "reviewer"]


@dataclass(frozen=True)
class CodeInfo:
    area: str
    error_class: str
    retry_class: str
    fix_action: str


def load_code_meta() -> dict[str, CodeInfo]:
    """{code: CodeInfo} from codes.py: the `# --- area ---` comment above each member and the
    `_meta()` table. Empty strings where the source has None."""
    source = CODES_PATH.read_text(encoding="utf-8")
    enums = _enum_classes(ENUMS_PATH)
    areas: dict[str, str] = {}
    in_class = False
    area = ""
    for line in source.splitlines():
        if line.startswith("class ErrorCode"):
            in_class = True
            continue
        if in_class and line.startswith("class "):
            break
        if not in_class:
            continue
        section = re.match(r"\s*# --- (.+?) ---", line)
        if section:
            area = section.group(1).strip()
        member = re.match(r"\s+([A-Z][A-Z0-9_]+) = \"([A-Z0-9_]+)\"", line)
        if member:
            areas[member.group(2)] = area
    tree = ast.parse(source)
    meta: dict[str, CodeInfo] = {}

    def enum_value(node: ast.expr) -> str:
        if isinstance(node, ast.Constant) and node.value is None:
            return ""
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return enums.get(node.value.id, {}).get(node.attr, node.attr.lower())
        raise SystemExit(f"{CODES_PATH}: unexpected meta value {ast.dump(node)}")

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_meta":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.value, ast.Dict):
                    for key, val in zip(stmt.value.keys, stmt.value.values, strict=True):
                        if not isinstance(key, ast.Attribute):
                            continue
                        args: list[ast.expr]
                        if isinstance(val, ast.Call):
                            args = list(val.args)
                        elif isinstance(val, ast.Tuple):
                            args = list(val.elts)
                        else:
                            raise SystemExit(f"{CODES_PATH}: unexpected meta for {key.attr}")
                        values = [enum_value(a) for a in args] + ["", "", ""]
                        code = enums.get("ErrorCode", {}).get(key.attr, key.attr)
                        meta[code] = CodeInfo(areas.get(code, ""), values[0], values[1], values[2])
    if not meta:
        raise SystemExit(f"{CODES_PATH}: no _meta() table found")
    return meta


# ---------------------------------------------------------------------------------------------
# Small text helpers.


def cell(text: Any) -> str:
    """One Markdown table cell: one line, pipes escaped."""
    s = "" if text is None else str(text)
    s = re.sub(r"\s*\n\s*", " ", s.strip())
    return s.replace("|", "\\|")


def prose(text: str | None) -> str:
    """A docstring as Markdown: hard-wrapped lines joined, blank-line paragraphs kept."""
    if not text:
        return ""
    paragraphs = re.split(r"\n\s*\n", text.strip())
    return "\n\n".join(re.sub(r"\s*\n\s*", " ", p.strip()) for p in paragraphs)


def slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, drop punctuation except `-` and `_`, spaces to `-`."""
    s = heading.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def code(text: str) -> str:
    return f"`{text}`"


# ---------------------------------------------------------------------------------------------
# Schema rendering.


class Spec:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.schemas: dict[str, Any] = raw.get("components", {}).get("schemas", {})

    def ref_name(self, ref: str) -> str:
        prefix = "#/components/schemas/"
        if not ref.startswith(prefix):
            raise SystemExit(f"unsupported $ref {ref}")
        return ref[len(prefix) :]

    def resolve(self, schema: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        if "$ref" in schema:
            name = self.ref_name(schema["$ref"])
            return name, self.schemas[name]
        return None, schema


ENUM_INLINE_MAX = 12
# Objects nested in many answers: their fields are listed once under "Shared objects" and the
# operation tables link there instead of repeating them.
SHARED_OBJECTS = ("ErrorReport",)
MAX_DEPTH = 6


def split_nullable(schema: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """The non-null members of an `anyOf`/`oneOf`, and whether null is allowed."""
    for key in ("anyOf", "oneOf"):
        if key in schema:
            members = [m for m in schema[key] if m.get("type") != "null"]
            return members, len(members) != len(schema[key])
    return [schema], schema.get("type") == "null"


def single(schema: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """The one non-null member (with the wrapper's description/default carried over) and the
    nullable flag; the schema itself when it is a real union."""
    members, nullable = split_nullable(schema)
    if len(members) == 1 and members[0] is not schema:
        inner = dict(members[0])
        for key in ("description", "default"):
            if key in schema and key not in inner:
                inner[key] = schema[key]
        return inner, nullable
    return schema, nullable


@dataclass
class Row:
    name: str
    type: str
    required: bool
    description: str
    constraints: str


@dataclass
class Renderer:
    spec: Spec
    error_code_anchor: str
    rows: list[Row] = field(default_factory=list)

    # -- type labels --------------------------------------------------------------------------

    def type_label(self, schema: dict[str, Any]) -> str:
        inner, nullable = single(schema)
        label = self._label(inner)
        return f"{label} or null" if nullable and label != "null" else label

    def _label(self, schema: dict[str, Any]) -> str:
        if "$ref" in schema:
            name, target = self.spec.resolve(schema)
            if "enum" in target:
                return f"{name} (enum)"
            return f"{name}"
        members, _ = split_nullable(schema)
        if members and members[0] is not schema:
            return " or ".join(sorted({self._label(m) for m in members}))
        if "const" in schema:
            return f"const {json.dumps(schema['const'])}"
        kind = schema.get("type")
        if kind == "array":
            return f"array of {self.type_label(schema.get('items', {}))}"
        if kind == "string":
            fmt = schema.get("format")
            if schema.get("contentMediaType") == "application/octet-stream":
                return "binary file"
            return f"string ({fmt})" if fmt else ("string (enum)" if "enum" in schema else "string")
        if kind == "object" or "properties" in schema or "patternProperties" in schema:
            if schema.get("properties"):
                return "object"
            values = schema.get("patternProperties") or {}
            if values:
                value_schema = next(iter(values.values()))
                return f"map of string to {self.type_label(value_schema)}"
            extra = schema.get("additionalProperties")
            if isinstance(extra, dict) and extra:
                return f"map of string to {self.type_label(extra)}"
            return "object (free-form)"
        if kind in ("integer", "number", "boolean", "null"):
            return str(kind)
        if not schema:
            return "any JSON value"
        return str(kind or "any")

    # -- constraints --------------------------------------------------------------------------

    def constraints(self, schema: dict[str, Any]) -> str:
        inner, _ = single(schema)
        parts: list[str] = []
        target = inner
        if "$ref" in inner:
            _, target = self.spec.resolve(inner)
            if "enum" not in target:
                target = {}
        enum = target.get("enum")
        if enum:
            if len(enum) > ENUM_INLINE_MAX:
                if target.get("title") == "ErrorCode":
                    parts.append(f"one of the [error codes](#{self.error_code_anchor})")
                else:
                    shown = ", ".join(code(str(v)) for v in enum[:ENUM_INLINE_MAX])
                    parts.append(f"one of: {shown}, ... ({len(enum)} values)")
            else:
                parts.append("one of: " + ", ".join(code(str(v)) for v in enum))
        for key, label in (
            ("minLength", "min length"),
            ("maxLength", "max length"),
            ("minimum", "min"),
            ("maximum", "max"),
            ("exclusiveMinimum", "greater than"),
            ("exclusiveMaximum", "less than"),
            ("minItems", "min items"),
            ("maxItems", "max items"),
            ("maxProperties", "max keys"),
        ):
            if key in inner:
                value = inner[key]
                if isinstance(value, float) and value.is_integer():
                    value = int(value)
                parts.append(f"{label} {value}")
        if "pattern" in inner:
            parts.append(f"pattern {code(inner['pattern'])}")
        for pattern in inner.get("patternProperties") or {}:
            parts.append(f"keys match {code(pattern)}")
        if "default" in schema and schema["default"] is not None:
            parts.append(f"default {code(json.dumps(schema['default']))}")
        elif "default" in inner and inner["default"] is not None:
            parts.append(f"default {code(json.dumps(inner['default']))}")
        if inner.get("writeOnly") or schema.get("writeOnly"):
            parts.append("write-only (never returned)")
        if inner.get("contentMediaType"):
            parts.append(f"content type {code(inner['contentMediaType'])}")
        return "; ".join(parts)

    # -- field tables -------------------------------------------------------------------------

    def object_of(self, schema: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
        """(component name, object schema) when `schema` is (nullably) one object with properties."""
        inner, _ = single(schema)
        name, target = self.spec.resolve(inner) if "$ref" in inner else (None, inner)
        if target.get("properties"):
            return name, target
        return None, None

    def flatten(
        self, schema: dict[str, Any], prefix: str, stack: tuple[str, ...], depth: int
    ) -> list[Row]:
        _, obj = self.object_of(schema)
        if obj is None:
            return []
        required = set(obj.get("required", []))
        rows: list[Row] = []
        for prop in sorted(obj["properties"]):
            sub = obj["properties"][prop]
            path = f"{prefix}{prop}"
            rows.extend(self.field_rows(sub, path, prop in required, stack, depth))
        return rows

    def field_rows(
        self,
        sub: dict[str, Any],
        path: str,
        required: bool,
        stack: tuple[str, ...],
        depth: int,
    ) -> list[Row]:
        inner, _ = single(sub)
        description = sub.get("description") or inner.get("description") or ""
        if "$ref" in inner and not description:
            _, target = self.spec.resolve(inner)
            if "enum" not in target:
                description = target.get("description", "")
        rows = [
            Row(path, self.type_label(sub), required, prose(description), self.constraints(sub))
        ]
        child_name, child = self.object_of(sub)
        child_prefix = f"{path}."
        if child is None and inner.get("type") == "array":
            child_name, child = self.object_of(inner.get("items", {}))
            child_prefix = f"{path}[]."
            sub_for_child = inner.get("items", {}) if child is not None else {}
        else:
            sub_for_child = sub
        if child is None:
            return rows
        if child_name and child_name in stack:
            rows[0].description = (
                rows[0].description + " (recursive: same shape as its parent)"
            ).strip()
            return rows
        if child_name in SHARED_OBJECTS and depth > 0:
            rows[0].description = (
                rows[0].description + f" Fields: see [{child_name}](#{slug(child_name)})."
            ).strip()
            return rows
        if depth >= MAX_DEPTH:
            rows[0].description = (
                rows[0].description + f" (fields: see `{child_name}` in the spec)"
            ).strip()
            return rows
        next_stack = stack + ((child_name,) if child_name else ())
        rows.extend(self.flatten(sub_for_child, child_prefix, next_stack, depth + 1))
        return rows

    def field_table(self, schema: dict[str, Any], expand_shared: bool = False) -> list[str]:
        name, obj = self.object_of(schema)
        if obj is None:
            return [
                "| Field | Type | Required | Description | Constraints |",
                "|---|---|---|---|---|",
                f"| (whole body) | {cell(self.type_label(schema))} | yes | "
                f"{cell(prose(schema.get('description', '')))} | {cell(self.constraints(schema))} |",
            ]
        rows = self.flatten(schema, "", (name,) if name else (), 0 if expand_shared else 1)
        lines = [
            "| Field | Type | Required | Description | Constraints |",
            "|---|---|---|---|---|",
        ]
        for row in rows:
            lines.append(
                f"| `{cell(row.name)}` | {cell(row.type)} | {'yes' if row.required else 'no'} | "
                f"{cell(row.description)} | {cell(row.constraints)} |"
            )
        return lines

    # -- examples -----------------------------------------------------------------------------

    def example(
        self, schema: dict[str, Any], name_hint: str = "", stack: tuple[str, ...] = ()
    ) -> Any:
        if "example" in schema:
            return schema["example"]
        if schema.get("examples"):
            return schema["examples"][0]
        if "const" in schema:
            return schema["const"]
        if "$ref" in schema:
            ref_name, target = self.spec.resolve(schema)
            if ref_name in stack:
                return {}
            return self.example(target, name_hint, (*stack, ref_name or ""))
        members, _ = split_nullable(schema)
        if members and members[0] is not schema:
            return self.example(members[0], name_hint, stack)
        if schema.get("default") is not None:
            return schema["default"]
        if schema.get("enum"):
            return schema["enum"][0]
        kind = schema.get("type")
        if kind == "object" or "properties" in schema or "patternProperties" in schema:
            props = schema.get("properties") or {}
            if props:
                return {p: self.example(props[p], p, stack) for p in sorted(props)}
            pattern_values = schema.get("patternProperties") or {}
            if pattern_values:
                return {"example_key": self.example(next(iter(pattern_values.values())), "", stack)}
            return {}
        if kind == "array":
            return [self.example(schema.get("items", {}), name_hint, stack)]
        if kind == "string":
            return self._string_example(schema, name_hint)
        if kind == "integer":
            return int(schema.get("minimum", 1) or 1)
        if kind == "number":
            return schema.get("minimum", 0.5) or 0.5
        if kind == "boolean":
            return False
        return None

    @staticmethod
    def _string_example(schema: dict[str, Any], name: str) -> str:
        fmt = schema.get("format")
        lowered = name.lower()
        if fmt == "uuid" or lowered.endswith("_id"):
            return "00000000-0000-4000-8000-000000000000"
        if fmt == "date-time":
            return "2026-10-05T09:30:00Z"
        if fmt == "date":
            return "2026-10-05"
        if fmt == "email" or lowered == "email":
            return "user@example.com"
        if fmt in ("uri", "url"):
            return "https://example.com"
        if "password" in lowered:
            return "<PASSWORD>"
        if lowered == "refresh_token":
            return "<REFRESH_TOKEN>"
        if lowered == "sku":
            return "SKU-001"
        if schema.get("contentMediaType"):
            return "<FILE>"
        text = "string"
        min_len = int(schema.get("minLength", 0) or 0)
        if min_len > len(text):
            text = "x" * min_len
        return text


# ---------------------------------------------------------------------------------------------
# Operations.


@dataclass
class Op:
    method: str
    path: str
    raw: dict[str, Any]
    tag: str

    @property
    def op_id(self) -> str:
        return str(self.raw.get("operationId", ""))

    @property
    def summary(self) -> str:
        return SUMMARY_OVERRIDES.get(self.op_id) or str(self.raw.get("summary", ""))

    @property
    def heading(self) -> str:
        return f"{self.method.upper()} {self.path}"

    @property
    def anchor(self) -> str:
        return slug(self.heading)

    @property
    def auth(self) -> bool:
        return bool(self.raw.get("security"))

    @property
    def action(self) -> str | None:
        return self.raw.get("x-mkos-action")

    def header(self, name: str) -> dict[str, Any] | None:
        for param in self.raw.get("parameters", []):
            if param.get("in") == "header" and param.get("name", "").lower() == name.lower():
                return dict(param)
        return None

    @property
    def idempotency_key(self) -> bool:
        return self.header("Idempotency-Key") is not None

    @property
    def if_match(self) -> bool:
        return self.header("If-Match") is not None

    def idempotent_label(self) -> str:
        if self.op_id in IDEMPOTENT_NOTES:
            return IDEMPOTENT_NOTES[self.op_id]
        if self.idempotency_key:
            return "yes (Idempotency-Key)"
        if self.method in ("get", "head", "options"):
            return "yes (read-only)"
        if self.method in ("put", "delete"):
            return "yes (HTTP method)"
        if self.if_match:
            return "no (guarded by If-Match)"
        return "no"

    def error_codes(self) -> list[tuple[str, list[str]]]:
        out: list[tuple[str, list[str]]] = []
        for status in sorted_statuses(self.raw.get("responses", {})):
            description = self.raw["responses"][status].get("description", "")
            codes = re.findall(r"`([A-Z][A-Z0-9_]+)`", description)
            if status == "default":
                codes = re.findall(r"(\d{3}) `([A-Z][A-Z0-9_]+)`", description)
                for st, c in codes:
                    out.append((st, [c]))
                continue
            if not status.startswith(("2", "3")) and codes:
                out.append((status, codes))
        return out


def sorted_statuses(responses: dict[str, Any]) -> list[str]:
    return sorted(responses, key=lambda s: (s == "default", s))


def collect_ops(spec: Spec) -> list[Op]:
    ops: list[Op] = []
    for path, item in spec.raw["paths"].items():
        for method, raw in item.items():
            if method not in HTTP_METHODS:
                continue
            tags = raw.get("tags") or ["health"]
            ops.append(Op(method, path, raw, tags[0]))
    ops.sort(key=lambda o: (tag_rank(o.tag), o.path, METHOD_ORDER[o.method]))
    return ops


def tag_rank(tag: str) -> tuple[int, str]:
    return (TAG_ORDER.index(tag), "") if tag in TAG_ORDER else (len(TAG_ORDER), tag)


def count_ops(raw: dict[str, Any]) -> int:
    return sum(1 for item in raw["paths"].values() for m in item if m in HTTP_METHODS)


def roles_label(action: str | None, matrix: dict[str, list[str]]) -> str:
    if action is None:
        return "none (public probe)"
    if action == "public":
        return "none (public)"
    if action == "platform:admin":  # ADR-0019: the user's flag, not a role
        return "`platform:admin`: platform admins only (`users.is_platform_admin`), any role"
    roles = matrix.get(action)
    if roles is None:
        raise SystemExit(f"x-mkos-action {action!r} is not an Action in {RBAC_PATH}")
    return f"`{action}`: {', '.join(roles)}"


def is_page(spec: Spec, schema: dict[str, Any]) -> bool:
    _, target = spec.resolve(schema) if "$ref" in schema else (None, schema)
    props = target.get("properties", {})
    return "data" in props and "page" in props


def success_schema(op: Op) -> tuple[str, dict[str, Any] | None, str | None]:
    for status in sorted_statuses(op.raw["responses"]):
        if status.startswith(("2", "3")):
            content = op.raw["responses"][status].get("content", {})
            for ctype in sorted(content):
                return status, content[ctype].get("schema"), ctype
            return status, None, None
    return "", None, None


# ---------------------------------------------------------------------------------------------
# The Markdown document.


def curl_example(
    op: Op, renderer: Renderer, body_example: Any, body_ctype: str | None
) -> list[str]:
    path = re.sub(r"\{([^}]+)\}", r"<\1>", op.path)
    query = [
        p for p in op.raw.get("parameters", []) if p.get("in") == "query" and p.get("required")
    ]
    if query:
        path += "?" + "&".join(f"{p['name']}=<{p['name']}>" for p in query)
    lines = [f'curl -X {op.method.upper()} "http://localhost:8000{path}"']
    if op.auth:
        lines.append('  -H "Authorization: Bearer <ACCESS_TOKEN>"')
    if op.idempotency_key:
        lines.append('  -H "Idempotency-Key: <UNIQUE_KEY>"')
    if op.if_match:
        lines.append("  -H 'If-Match: \"<ETAG>\"'")
    if body_ctype == "application/json" and body_example is not None:
        lines.append('  -H "Content-Type: application/json"')
        payload = json.dumps(body_example, separators=(",", ":"), ensure_ascii=False)
        lines.append("  -d '" + payload.replace("'", "'\\''") + "'")
    elif body_ctype == "multipart/form-data" and isinstance(body_example, dict):
        for key, value in body_example.items():
            if value == "<FILE>":
                lines.append(f'  -F "{key}=@./<PATH_TO_FILE>"')
            elif value is not None:
                lines.append(f'  -F "{key}={value}"')
    return ["```bash", " \\\n".join(lines), "```"]


APPLIED_FIXES: list[tuple[str, str, str, str, str, str]] = []


def apply_example_fixes(raw: dict[str, Any]) -> None:
    """Correct the EXAMPLE_FIXES values in the loaded spec (in memory only)."""
    by_id = {
        o.get("operationId"): o
        for item in raw["paths"].values()
        for m, o in item.items()
        if m in HTTP_METHODS
    }
    APPLIED_FIXES.clear()
    for fix in EXAMPLE_FIXES:
        op_id, st, field_name, wrong, right, _ = fix
        if op_id not in by_id:
            raise SystemExit(f"EXAMPLE_FIXES names an operation the spec lacks: {op_id}")
        content = by_id[op_id].get("responses", {}).get(st, {}).get("content", {})
        example = content.get("application/json", {}).get("example")
        if not isinstance(example, dict) or field_name not in example:
            raise SystemExit(f"EXAMPLE_FIXES: {op_id} {st} has no example field {field_name}")
        if example[field_name] == right:
            continue
        if example[field_name] != wrong:
            raise SystemExit(
                f"EXAMPLE_FIXES: {op_id} {st} {field_name} is neither value; update it"
            )
        example[field_name] = right
        APPLIED_FIXES.append(fix)


def op_fixes(op: Op) -> list[tuple[str, str, str, str, str, str]]:
    return [f for f in APPLIED_FIXES if f[0] == op.op_id]


def render_op(op: Op, spec: Spec, renderer: Renderer, matrix: dict[str, list[str]]) -> list[str]:
    raw = op.raw
    out = [f"### {op.heading}", ""]
    summary = op.summary
    if summary:
        out += [f"**{summary.rstrip('.')}.**", ""]
    description = prose(raw.get("description"))
    if description and description != summary:
        out += [description, ""]
    status, schema, _ = success_schema(op)
    success = f"{status} " + (
        code(renderer.type_label(schema)) if schema is not None else "(no body)"
    )
    out += [
        "| | |",
        "|---|---|",
        f"| Operation id | `{op.op_id}` |",
        f"| Auth | {'Bearer access token' if op.auth else 'none'} |",
        f"| Permission (roles that hold it) | {roles_label(op.action, matrix)} |",
        f"| Idempotent | {op.idempotent_label()} |",
        f"| Success | {success} |",
        "",
    ]

    params = raw.get("parameters", [])
    out += ["#### Parameters", ""]
    if params:
        out += [
            "| Name | In | Type | Required | Description | Constraints |",
            "|---|---|---|---|---|---|",
        ]
        order = {"path": 0, "query": 1, "header": 2, "cookie": 3}
        for p in sorted(params, key=lambda p: (order.get(p.get("in"), 9), p.get("name", ""))):
            s = p.get("schema", {})
            out.append(
                f"| `{cell(p['name'])}` | {p['in']} | {cell(renderer.type_label(s))} | "
                f"{'yes' if p.get('required') else 'no'} | "
                f"{cell(prose(p.get('description') or s.get('description', '')))} | "
                f"{cell(renderer.constraints(s))} |"
            )
    else:
        out.append("None.")
    out.append("")

    out += ["#### Request body", ""]
    body = raw.get("requestBody")
    body_example: Any = None
    body_ctype: str | None = None
    if body:
        for body_ctype in sorted(body.get("content", {})):
            media = body["content"][body_ctype]
            body_schema = media.get("schema", {})
            required = "required" if body.get("required") else "optional (the body may be omitted)"
            out += [
                f"Content type `{body_ctype}`, {required}. Schema: "
                f"{code(renderer.type_label(body_schema))}.",
                "",
            ]
            out += renderer.field_table(body_schema)
            out.append("")
            if "example" in media:
                body_example = media["example"]
                label = "Example"
            else:
                body_example = renderer.example(body_schema)
                label = "Example (built from the schema; values are placeholders)"
            if body_ctype == "application/json":
                out += [
                    f"{label}:",
                    "",
                    "```json",
                    json.dumps(body_example, indent=2, ensure_ascii=False),
                    "```",
                    "",
                ]
            break
    else:
        out += ["None.", ""]

    out += ["#### Example call", ""]
    out += curl_example(op, renderer, body_example, body_ctype)
    out.append("")

    out += ["#### Responses", ""]
    out += [
        "| Status | Meaning | Body | Headers | Error codes |",
        "|---|---|---|---|---|",
    ]
    responses = raw.get("responses", {})
    for st in sorted_statuses(responses):
        resp = responses[st]
        description = resp.get("description", "")
        content = resp.get("content", {})
        body_label = ""
        for media_type in sorted(content):
            sch = content[media_type].get("schema")
            if sch is not None:
                body_label = code(renderer.type_label(sch))
            break
        headers = ", ".join(code(h) for h in sorted(resp.get("headers", {})))
        meaning = re.sub(r"\s*Codes:.*$", "", description, flags=re.S).strip()
        if meaning == "Successful Response":
            meaning = "Success."
        codes = re.findall(r"`([A-Z][A-Z0-9_]+)`", description) if "Codes:" in description else []
        if st == "default":
            codes = []
        code_cell = ", ".join(f"`{c}`" for c in codes)
        out.append(
            f"| {st} | {cell(meaning)} | {cell(body_label)} | {cell(headers)} | {cell(code_cell)} |"
        )
    out.append("")
    enveloped = [
        s
        for s in responses
        if not s.startswith(("2", "3"))
        and any(
            m.get("schema", {}).get("$ref", "").endswith("/ErrorEnvelope")
            for m in responses[s].get("content", {}).values()
        )
    ]
    if enveloped:
        others = [s for s in responses if not s.startswith(("2", "3")) and s not in enveloped]
        sentence = "Every error status above answers with the [error envelope](#the-error-envelope)"
        if others:
            sentence += f", except {', '.join(others)}"
        out.append(sentence + ".")
    elif any(not s.startswith(("2", "3")) for s in responses):
        out.append(
            "The error status above answers plain JSON (its example below), not the "
            "[error envelope](#the-error-envelope)."
        )
    out.append("")
    notes = CODE_NOTES.get(op.op_id, [])
    if notes:
        out += [
            "**Also returned.** The code returns these too; the contract does not list them yet:",
            "",
            "| Status | Error codes | When | Source |",
            "|---|---|---|---|",
        ]
        for st, note_codes, when, source in notes:
            out.append(
                f"| {st} | {', '.join(code(c) for c in note_codes)} | {cell(when)} | {cell(source)} |"
            )
        out.append("")
    if op.op_id in OP_NOTES:
        out += [OP_NOTES[op.op_id], ""]
    for fix in op_fixes(op):
        _, st, field_name, wrong, right, source = fix
        out += [
            f"> **Note.** The contract's {st} example shows `{field_name}` as `{wrong}`. The "
            f"running API returns `{right}` (source: `{source}`); the example below and "
            "`swagger.html` show the corrected value. The local mock (`make mock`) still answers "
            "the contract's value.",
            "",
        ]

    for st in sorted_statuses(responses):
        if not st.startswith(("2", "3")):
            for media_type, media in sorted(responses[st].get("content", {}).items()):
                if "schema" not in media and "example" in media:
                    out += [
                        f"{st} example (`{media_type}`):",
                        "",
                        "```json",
                        json.dumps(media["example"], indent=2, ensure_ascii=False),
                        "```",
                        "",
                    ]
            continue
        resp = responses[st]
        for header, spec_header in sorted(resp.get("headers", {}).items()):
            out += [f"Header `{header}` on {st}: {prose(spec_header.get('description', ''))}", ""]
        content = resp.get("content", {})
        for media_type in sorted(content):
            media = content[media_type]
            sch = media.get("schema")
            note = BODY_NOTES.get((op.op_id, st))
            if note is not None:
                out += [
                    f"{st} body (from the code; the contract declares no fields):",
                    "",
                    "```json",
                    json.dumps(note, indent=2, ensure_ascii=False),
                    "```",
                    "",
                ]
                continue
            if sch is None:
                continue
            out += [f"**{st} body fields** (`{media_type}`):", ""]
            out += renderer.field_table(sch)
            out.append("")
            if "example" in media:
                out += [
                    f"{st} example:",
                    "",
                    "```json",
                    json.dumps(media["example"], indent=2, ensure_ascii=False),
                    "```",
                    "",
                ]
    out += ["[Back to the summary](#all-operations)", ""]
    return out


CONVENTIONS = r"""
## Conventions every call follows

These rules hold for every operation below. The per-operation sections do not repeat them.

### Authentication

Every `/api/v1` route except `auth/login`, `auth/refresh`, `auth/logout` and the OAuth callback needs an **access token** in the `Authorization` header:

```http
Authorization: Bearer <ACCESS_TOKEN>
```

- **Log in.** `POST /api/v1/auth/login` with `{"email", "password"}` returns `access_token`, `refresh_token`, `expires_in` (seconds) and the user's `memberships`. The active tenant is the one the user used most recently. Every refusal (a wrong password, an unknown email, and so on) is the same `401 UNAUTHENTICATED`, so the answer never tells an attacker which part was wrong. Login is limited to 5 tries a minute per IP and email.
- **Token lifetimes.** The access token lives 30 minutes and the refresh token 14 days (the defaults of `ACCESS_TOKEN_TTL_MINUTES` and `REFRESH_TOKEN_TTL_DAYS`). Read `expires_in` instead of hard-coding it.
- **Refresh.** `POST /api/v1/auth/refresh` with `{"refresh_token"}` returns a new pair and the old refresh token stops working (rotation). Reusing an already rotated refresh token revokes the whole chain (the token "family") and answers 401, because it means a token was stolen or copied. So **serialise refreshes** in the client: two tabs refreshing the same token at once count as reuse and log the user out.
- **Log out.** `POST /api/v1/auth/logout` with `{"refresh_token"}` revokes the family and always answers `204`. An access token already issued stays valid until it expires, so drop it from memory.
- **Who am I.** `GET /api/v1/auth/me` returns the user, the active tenant, the role and all memberships.
- **Switch tenant.** `POST /api/v1/auth/switch-tenant` with `{"target_tenant_id"}` returns a brand-new token pair (a new family) for another tenant the user is an active member of. A tenant the user is not in is `404`. Replace both tokens; the old ones keep pointing at the old tenant.
- **401 vs 403.** A missing, expired or invalid token is `401 UNAUTHENTICATED` (with `WWW-Authenticate: Bearer`). A valid token whose role lacks the permission is `403 PERMISSION_DENIED`.

```mermaid
sequenceDiagram
  participant FE as Frontend
  participant API as API /api/v1
  FE->>API: POST /auth/login (email, password)
  API-->>FE: 200 access_token, refresh_token, expires_in, memberships
  FE->>API: GET /listings (Authorization: Bearer access)
  API-->>FE: 200 page of listings
  Note over FE,API: access token expires after expires_in seconds
  FE->>API: GET /listings (expired token)
  API-->>FE: 401 UNAUTHENTICATED
  FE->>API: POST /auth/refresh (refresh_token)
  API-->>FE: 200 new access_token and new refresh_token
  FE->>API: GET /listings (retry once, new access token)
  API-->>FE: 200 page of listings
  FE->>API: POST /auth/switch-tenant (target_tenant_id)
  API-->>FE: 200 new token pair for the other tenant
  FE->>API: POST /auth/logout (refresh_token)
  API-->>FE: 204
```

How to read it: time runs top to bottom. Solid arrows are requests from the frontend, dashed arrows are the API's answers. The middle part shows the normal recovery from an expired access token: one refresh, then retry the original call once. If the refresh itself answers 401, the session is over: send the user to the login screen. The last two calls show a tenant switch and a log out.

<sub>Source: apps/api/app/api/v1/routers/auth.py, apps/api/app/api/v1/deps.py, apps/api/app/services/auth.py, apps/api/app/core/config.py, apps/api/app/api/rate_limit.py</sub>

### Roles and permissions

Each operation requires one **action** (shown as "Permission" in every operation, from the `x-mkos-action` field of the spec). A member may call it only if their role holds that action. The role comes from the membership row in the database, never from the token, so a role change applies on the next request.

@@ROLE_MATRIX@@

<sub>Source: apps/api/app/core/rbac.py, apps/api/app/api/v1/deps.py</sub>

### Tenant scoping

A **tenant** is one brand. The tenant of every request is the `tid` inside the access token; no route accepts a `tenant_id` in the path, query or body (the one exception is `target_tenant_id` on `auth/switch-tenant`, which is checked against the caller's own memberships). The database enforces the same boundary with row-level security, so a bug in one query still cannot leak another tenant's rows.

An object that belongs to another tenant answers **`404 NOT_FOUND`, never `403`**, so the API never confirms that someone else's id exists.

<sub>Source: apps/api/app/api/v1/deps.py (get_current_principal binds the tenant), apps/api/app/api/v1/routers/auth.py (switch-tenant), docs/architecture/DATABASE.md (RLS)</sub>

### Idempotency-Key

Operations that start background work or change several rows require an `Idempotency-Key` header, so a retry after a network error never does the work twice.

- **Value:** 1 to 255 visible ASCII characters. Use a fresh UUID per user action, and reuse it only to retry that same action.
- **Replay:** the same key, the same method and path, the same body and the same member within **24 hours** returns the stored first answer (same status, same body) without running the work again. A stored `422` is replayed too.
- **Mismatch:** the same key with a different body, a different endpoint or a different member is `409 IDEMPOTENCY_KEY_REUSED`.
- **Failures are not stored:** if the first call failed with an error, the key is released, and a retry with the same key runs again.
- **Expiry:** after 24 hours the key counts as new.
- For a multipart upload, the "body" that is compared is the bytes of the `file` part, not the raw multipart body (its boundary is random).

These operations require it:

@@IDEMPOTENT_OPS@@

<sub>Source: apps/api/app/api/v1/deps.py (IdempotencyKey), apps/api/app/services/idempotency.py, apps/api/app/repositories/idempotency.py</sub>

### ETag and If-Match (optimistic concurrency)

Two resources can be edited by several people at once. Their GET (and every successful PATCH) returns an `ETag` header: the current version id as a quoted UUID, for example `"5f0c7c1e-8d2b-5a4e-9b6f-3c2d1e0f9a8b"`. Send it back as `If-Match` on the next PATCH.

- A stale `If-Match` (someone saved in between) is **`409 STALE_VERSION`**. Re-read the resource, show the user the new content, and let them re-apply their change.
- A missing `If-Match`, or one that is not a quoted UUID, is **`422 VALIDATION_FAILED`** (the header is required).
- This API does **not** use `412 Precondition Failed` or `428 Precondition Required`.
- For a listing, the `ETag` equals `current_version_id` in the body, and each content PATCH creates a new listing version.

@@IF_MATCH_OPS@@

<sub>Source: apps/api/app/api/v1/deps.py (IfMatch, IF_MATCH_PATTERN), apps/api/app/api/v1/routers/listings.py, apps/api/app/api/v1/routers/tenants.py</sub>

### Cursor pagination

Every list operation answers the same shape, and pages with an opaque cursor (there is no offset or page number):

```json
{
  "data": [ { "...": "one item" } ],
  "page": { "next_cursor": "<OPAQUE_CURSOR>", "has_more": true, "limit": 50 }
}
```

- Query parameters: `limit` (1 to 200, default 50) and `cursor` (the `page.next_cursor` of the previous page; leave it out for the first page).
- Stop when `has_more` is `false` (then `next_cursor` is `null`).
- Treat the cursor as an opaque string. Do not build or edit one: an invalid cursor is `422` with `details[0].loc = ["query", "cursor"]`. Keep the same filters while you page. Most lists are newest first. The exceptions: members and rules are oldest first, import rows come in the CSV's row order, and `GET /api/v1/listings` filtered to `status=validated` or `status=needs_review` (the review queue) pages oldest first by the time the listing entered that status, so the longest-waiting listing comes first. The review queue refuses a cursor made for the other order (`422`).

These operations are paginated:

@@PAGED_OPS@@

<sub>Source: apps/api/app/api/pagination.py, apps/api/app/repositories/listings.py (QUEUE_STATUSES), apps/api/app/repositories/members.py, apps/api/app/repositories/rules.py, apps/api/app/repositories/imports.py</sub>

### The error envelope

Every non-2xx answer on every `/api/v1` route (including 404 for an unknown path, 405, 413 and 500) has this one shape. The only exception is the `503` of `/readyz`, a fixed probe body (see [`GET /readyz`](#get-readyz)).

```json
@@ERROR_EXAMPLE@@
```

| Field | Meaning |
|---|---|
| `error.type` | The machine-readable error code. Branch on this, never on `message`. All codes are listed in [Error codes](#error-codes). |
| `error.message` | A short fixed text that is safe to show. It never echoes your input. |
| `error.status` | The HTTP status again, for logging. |
| `error.correlation_id` | The request id (same as the `X-Request-ID` response header). Log it and quote it in bug reports. |
| `error.details` | Only on validation errors: a list of `{loc, msg}`, where `loc` is the path to the bad input, for example `["body", "items", 0, "listing_id"]` or `["query", "cursor"]`. At most 50 items. `null` otherwise. |
| `error.error_report` | The shared `ErrorReport` object (the same shape the Rules Engine uses for listing problems), so one UI component can render both. |

Status codes you will see: `400` malformed request, `401` not authenticated, `403` role lacks the permission (or maker-checker), `404` not found or another tenant's, `405` wrong method (with an `Allow` header), `409` conflict with the current state, `413` body too large, `422` validation failed, `429` rate limited, `500` internal error (never with details), `501` the marketplace is switched off or not configured, `502`/`503` the marketplace answered badly or is down (OAuth callback only; `/readyz` also answers 503 when the database is down).

<sub>Source: apps/api/app/api/errors.py, packages/schemas/src/mkos_schemas/codes.py</sub>

### Request size limits

A request body over 10 MiB (`REQUEST_MAX_BODY_BYTES`, default 10,485,760 bytes) is refused with `413 PAYLOAD_TOO_LARGE` before any processing. `POST /api/v1/rules/dry-run` has a tighter cap of 256 KiB. A malformed `Content-Length` is `400`.

<sub>Source: apps/api/app/api/body_limit.py, apps/api/app/main.py, apps/api/app/api/v1/routers/rules.py</sub>

### Rate limits and Retry-After

| Limit | Applies to | Default |
|---|---|---|
| Per tenant | every authenticated request, counted after the token and membership check pass | 120 per minute (`RATE_LIMIT_PER_TENANT_PER_MIN`) |
| Per IP | every public route (login, refresh, logout, OAuth callback) | 60 per minute (`RATE_LIMIT_PER_IP_PER_MIN`) |
| Login | `POST /auth/login` and `POST /signup/status`, per IP and email | 5 per minute (`AUTH_LOGIN_RATE_LIMIT_PER_MIN`) |
| Login | `POST /auth/login` and `POST /signup/status`, per email from any address | 10 per minute (`AUTH_LOGIN_EMAIL_RATE_LIMIT_PER_MIN`) |
| Sign-up | `POST /signup`, per IP | 5 per minute (`AUTH_LOGIN_RATE_LIMIT_PER_MIN`) |
| Status refresh | `POST /listings/{listing_id}/refresh-status`, per listing | once per 5 minutes |
| Per-tenant caps | live imports, live image jobs, live brand document jobs, documents and chunks per tenant | fixed caps |
| Daily AI budget | `listings/generate`, `listings/{listing_id}/regenerate`, and (per item) a reject with `regenerate` in `listings/approve` | a daily spend cap per tenant (`LLM_TENANT_DAILY_BUDGET_USD`); answers `429 BUDGET_EXCEEDED` |

The windows are fixed calendar minutes. Every 429 carries a `Retry-After` header in seconds: the time until the minute window resets, the time left on a cap with a known end, 30 seconds for a cap with no known end, or the time to the next midnight UTC for `BUDGET_EXCEEDED`. Wait at least that long before retrying, and do not retry in a tight loop.

<sub>Source: apps/api/app/api/rate_limit.py, apps/api/app/services/rate_limit.py, apps/api/app/core/errors.py (CAP_RETRY_AFTER_S), apps/api/app/core/config.py, apps/api/app/services/publish.py (REFRESH_WINDOW_S), apps/api/app/services/budget.py, apps/api/app/services/imports/cap.py, apps/api/app/services/images.py, apps/api/app/services/brand_documents.py</sub>

### Request ids (X-Request-ID)

Every response carries exactly one `X-Request-ID` header. If you send an `X-Request-ID` that is 1 to 128 characters of `A-Z a-z 0-9 . _ : -`, the API keeps it; otherwise it makes a new UUID. The same id appears as `error.correlation_id` in errors and as `request_id` on jobs started by that request, and it is in every server log line. Send one per user action to trace it end to end.

<sub>Source: apps/api/app/core/middleware.py, apps/api/app/core/request_context.py</sub>

### Background work: 202 and job polling

Slow work (AI generation, publishing, CSV import, image checks, brand document embedding, status refresh) does not finish inside the request, because it can take from seconds to minutes and must survive a restart. The API answers **`202 Accepted`** at once with a body that carries a `job_id` and a `poll_url` (`/api/v1/jobs/{job_id}`).

- `POST /api/v1/listings/publish` is a batch: its 202 body has one item per listing, each with its own `job_id` (or an `error`). Its top-level `poll_url` names only the first job, so poll each item's job.
- Poll `GET /api/v1/jobs/{job_id}` about every 1 to 2 seconds (the end-to-end tests poll every second). `estimated_duration_seconds`, present on some 202 bodies, is a hint for a progress bar.
- `status` moves `queued` -> `running` -> one terminal state: `succeeded`, `partial` (some items failed), `failed`, `cancelled` or `dead_letter` (gave up after its retries). A temporary failure (for example the marketplace timed out) sends the job from `running` back to `queued` for another attempt, so `queued` can come again after `running`. Stop polling only at a terminal state.
- `progress_pct` (0 to 100) and `stage` show progress; `attempt` / `max_attempts` show retries. On failure, `error` holds an `ErrorReport`.
- `POST /api/v1/jobs/{job_id}/cancel` stops a job that is still `queued`; a job that is no longer `queued` is `409 INVALID_STATUS_TRANSITION`.
- A job of another tenant is `404`, like every other object.

```mermaid
sequenceDiagram
  participant FE as Frontend
  participant API as API
  participant DB as Postgres
  participant W as Worker
  FE->>API: POST /listings/generate (Idempotency-Key)
  API->>DB: job_runs row and queue entry, one transaction
  API-->>FE: 202 job_id, poll_url, status queued
  W->>DB: claim the job, status running
  loop every 1 to 2 seconds
    FE->>API: GET /jobs/job_id
    API-->>FE: 200 status queued or running, progress_pct
  end
  W->>DB: save the result, status succeeded
  FE->>API: GET /jobs/job_id
  API-->>FE: 200 status succeeded, result_url
  FE->>API: GET /listings/listing_id
  API-->>FE: 200 listing with its new content
```

How to read it: the request returns before the work is done. The job is written to a queue table in the same database and in the same transaction as the request's other writes, so a job is never lost between the API and the worker. The worker runs it and saves the result; the frontend polls the job, then reads the finished resource (the job's `listing_id` or `result_url`).

<sub>Source: apps/api/app/api/v1/routers/jobs.py, apps/api/app/services/jobs.py, apps/api/app/workers/wrapper.py (retry back to queued), apps/api/app/services/publish.py (BatchAccepted poll_url), packages/schemas/src/mkos_schemas/jobs.py, packages/schemas/src/mkos_schemas/enums.py (JobState), scripts/e2e/generate.sh</sub>

### CORS

Browsers may call the API only from the exact origins in `ALLOWED_ORIGINS` (comma-separated). Staging and production refuse to start with `*`, an http or loopback origin, or a trailing `/`: every entry must be an exact `https://host[:port]`. The local default is `http://localhost:3000`; a Vite dev server on `http://localhost:5173` must be added to the backend's `.env`. Credentials (cookies) are off: auth is the Bearer header only. Allowed methods are the ones the API serves (`GET`, `POST`, `PATCH`, `DELETE`), allowed request headers are `Authorization`, `Content-Type`, `Idempotency-Key`, `If-Match` and `X-Request-ID`, and the readable response headers are `ETag`, `Location`, `Retry-After` and `X-Request-ID`. Preflights are cached for 10 minutes. A preflight from an origin that is not allowed is `400` in the error envelope.

<sub>Source: apps/api/app/api/cors.py, apps/api/app/core/config.py, docs/canon/ENV.md</sub>

### Live Swagger on a running server

On `local` and `staging` the API itself serves Swagger UI at `/docs` and the spec at `/openapi.json` (for example `http://localhost:8000/docs`). "Try it out" works there, because the page and the API share one origin. Production serves neither. The copy next to this file, `swagger.html`, opens from disk anywhere because the spec is embedded in it (its Swagger UI scripts come from a CDN); from disk it can show the API but not call it.

<sub>Source: apps/api/app/main.py</sub>
"""


def role_matrix_table(matrix: dict[str, list[str]], ops: list[Op]) -> str:
    lines = [
        "| Action | " + " | ".join(ROLE_ORDER) + " | Operations |",
        "|---|" + "---|" * (len(ROLE_ORDER) + 1),
    ]
    for action in sorted(matrix):
        roles = set(matrix[action])
        using = [o for o in ops if o.action == action]
        marks = " | ".join("yes" if r in roles else "-" for r in ROLE_ORDER)
        lines.append(f"| `{action}` | {marks} | {len(using)} |")
    lines.append("")
    lines.append(
        "Public operations (`x-mkos-action: public`) need no token. `/healthz` and `/readyz` carry "
        "no action at all. A role not in the table holds nothing. The sign-up queue "
        "(`x-mkos-action: platform:admin`, ADR-0019) is not in the table: it needs the caller's "
        "own `users.is_platform_admin` flag, whatever their role."
    )
    return "\n".join(lines)


def op_link_list(ops: list[Op]) -> str:
    if not ops:
        return "- (none)"
    return "\n".join(f"- [`{o.heading}`](#{o.anchor}) (`{o.op_id}`)" for o in ops)


def error_example(spec: Spec) -> str:
    for item in spec.raw["paths"].values():
        for raw in item.values():
            media = (
                raw.get("responses", {}).get("422", {}).get("content", {}).get("application/json")
            )
            if media and "example" in media:
                example = copy.deepcopy(media["example"])
                if example.get("error", {}).get("details") is None:
                    example["error"]["details"] = [
                        {
                            "loc": ["body", "items", 0, "listing_id"],
                            "msg": "Input should be a valid UUID.",
                        }
                    ]
                return json.dumps(example, indent=2, ensure_ascii=False)
    raise SystemExit("openapi.json has no 422 example to show the envelope")


def error_codes_section(spec: Spec, meta: dict[str, CodeInfo], ops: list[Op]) -> list[str]:
    codes = spec.schemas.get("ErrorCode", {}).get("enum", [])
    used: dict[str, list[str]] = {}
    for op in ops:
        found = [c for _, op_codes in op.error_codes() for c in op_codes]
        found += [c for note in CODE_NOTES.get(op.op_id, []) for c in note[1]]
        for c in found:
            if c not in codes:
                raise SystemExit(f"{op.op_id}: {c} is not in the ErrorCode enum")
            used.setdefault(c, [])
            if op.op_id not in used[c]:
                used[c].append(op.op_id)
    out = [
        "## Error codes",
        "",
        "Every value `error.type` (and `error_code` inside an `ErrorReport`) can take. The list is the "
        "`ErrorCode` enum in the spec. The area, class and retry class come from the one code registry, "
        "`packages/schemas/src/mkos_schemas/codes.py`, which is where to look for the authoritative list.",
        "",
        "How to act on the retry class:",
        "",
        "- `auto_retry`: a temporary problem. Retry later (respect `Retry-After` when present).",
        "- `user_action`: the user must change something (fix the input, reconnect an account, ask someone else to approve).",
        '- `ai_correct`: a content problem the system fixes itself by regenerating; show it as "being fixed".',
        "- `system_failure`: our side failed. Show a generic error and report the `correlation_id`.",
        "",
        '"Returned directly by" lists the operations whose responses name the code as `error.type`: '
        'declared in the contract, or listed under "Also returned" in the operation. An empty cell '
        "means no response declares it there; such a code usually reaches you inside an "
        "`ErrorReport` instead (a batch item's `error`, a job's `error`, a validation result, an "
        "import row or an issue). The contract lists the codes of the common cases; a status can "
        "carry another code from this table, so always fall back on the HTTP status for a code "
        "your screen does not handle.",
        "",
        "| Code | Area | Class | Retry class | Fix action | Returned directly by |",
        "|---|---|---|---|---|---|",
    ]
    for c in sorted(codes):
        info = meta.get(c, CodeInfo("", "", "", ""))
        ops_cell = ", ".join(f"`{o}`" for o in sorted(used.get(c, [])))
        if len(used.get(c, [])) > 12:
            ops_cell = f"{len(used[c])} operations"
        out.append(
            f"| `{c}` | {cell(info.area)} | {cell(info.error_class)} | {cell(info.retry_class)} | "
            f"{cell(info.fix_action)} | {cell(ops_cell)} |"
        )
    out.append("")
    missing = sorted(set(codes) - set(meta))
    if missing:
        out += [f"Codes with no registry entry: {', '.join(code(c) for c in missing)}.", ""]
    return out


def build_markdown(
    spec: Spec, matrix: dict[str, list[str]], meta: dict[str, CodeInfo]
) -> tuple[str, int]:
    info = spec.raw.get("info", {})
    version = info.get("version", "")
    ops = collect_ops(spec)
    renderer = Renderer(spec, error_code_anchor="error-codes")

    op_ids = {o.op_id for o in ops}
    for table_name, keys in (
        ("IDEMPOTENT_NOTES", set(IDEMPOTENT_NOTES)),
        ("SUMMARY_OVERRIDES", set(SUMMARY_OVERRIDES)),
        ("BODY_NOTES", {k[0] for k in BODY_NOTES}),
        ("CODE_NOTES", set(CODE_NOTES)),
        ("OP_NOTES", set(OP_NOTES)),
    ):
        unknown = sorted(keys - op_ids)
        if unknown:
            raise SystemExit(f"{table_name} names operations the spec lacks: {unknown}")
    anchors = [o.anchor for o in ops]
    if len(set(anchors)) != len(anchors):
        raise SystemExit("two operations produce the same heading anchor")

    out: list[str] = [
        f"# AI Marketplace OS API reference (v{version})",
        "",
        "> Generated from `packages/schemas/jsonschema/openapi.json` by "
        "`scripts/docs/gen_api_reference.py`. Do not edit by hand: change the code, export the "
        "contract, then run `make docs-handover`.",
        "",
        "This is the complete reference of the backend's HTTP API for the frontend and AI teams. "
        f"It lists all {len(ops)} operations: what each one does, who may call it, what to send and "
        "what comes back. The same contract ships as generated TypeScript types in the npm package "
        "`@maniacultasset/schemas` (GitHub Packages), which the frontend installs under the alias "
        "`@mkos/schemas`, so never hand-write a request or response type.",
        "",
        "- Read **Conventions** first: auth, tenants, idempotency, concurrency, paging, errors and "
        "jobs work the same everywhere.",
        "- Then use the **summary table** to find an operation, and its section for the details.",
        "- For an interactive view, open [`swagger.html`](swagger.html) in a browser. It opens "
        "straight from disk because the spec is embedded in it; the Swagger UI scripts load from "
        "`cdn.jsdelivr.net`, so the browser needs internet access.",
        "- For the bigger picture, read the other handover documents in this folder: "
        "[`ARCHITECTURE.md`](ARCHITECTURE.md) (how the backend is built and deployed), "
        "[`DATABASE.md`](DATABASE.md) (tables, relations and the listing status machine) and "
        "[`FRONTEND-GUIDE.md`](FRONTEND-GUIDE.md) (screen by screen integration).",
        "",
        "## Base URLs",
        "",
        "Every path below already includes its prefix: all routes are under `/api/v1` except "
        "`/healthz` and `/readyz`. The full URL is the base URL plus the path, for example "
        "`http://localhost:8000/api/v1/auth/login`.",
        "",
        "| Environment | Base URL | Notes |",
        "|---|---|---|",
    ]
    for env, url, note in BASE_URLS:
        out.append(f"| {env} | `{url}` | {note} |")
    out += [
        "",
        "## How to regenerate",
        "",
        "```bash",
        "make docs-handover",
        "```",
        "",
        "It rewrites this file and `swagger.html` from the committed `openapi.json`. When an API "
        "changes, first re-export the contract (`make -C packages/schemas all`, checked by "
        "`make schemas-drift`), then run `make docs-handover`. The generator fails if any operation "
        "in the spec is missing from the summary table. `python scripts/docs/gen_api_reference.py "
        "--check` exits 1 when the committed files are out of date.",
        "",
        "## Words used in this reference",
        "",
        "| Term | Meaning |",
        "|---|---|",
    ]
    enum_text = {
        "@@LISTING_STATUS@@": ", ".join(code(v) for v in spec.schemas["ListingStatus"]["enum"]),
        "@@MARKETPLACE_STATE@@": ", ".join(
            code(v) for v in spec.schemas["MarketplaceState"]["enum"]
        ),
    }
    for term, meaning in GLOSSARY:
        for key, value in enum_text.items():
            meaning = meaning.replace(key, value)
        if "@@" in meaning:
            raise SystemExit(f"an unfilled placeholder is left in the glossary entry {term}")
        out.append(f"| **{term}** | {meaning} |")
    out.append("")

    conventions = CONVENTIONS.strip("\n")
    conventions = conventions.replace("@@ROLE_MATRIX@@", role_matrix_table(matrix, ops))
    conventions = conventions.replace(
        "@@IDEMPOTENT_OPS@@", op_link_list([o for o in ops if o.idempotency_key])
    )
    conventions = conventions.replace(
        "@@IF_MATCH_OPS@@",
        "These operations require `If-Match`:\n\n" + op_link_list([o for o in ops if o.if_match]),
    )
    paged = []
    for o in ops:
        _, sch, _ = success_schema(o)
        if sch is not None and is_page(spec, sch):
            paged.append(o)
    conventions = conventions.replace("@@PAGED_OPS@@", op_link_list(paged))
    conventions = conventions.replace("@@ERROR_EXAMPLE@@", error_example(spec))
    if "@@" in conventions:
        raise SystemExit("an unfilled placeholder is left in the conventions")
    out += [conventions, "", JOURNEY.strip("\n"), ""]

    # Summary table.
    out += [
        "## All operations",
        "",
        "Auth: `yes` means a Bearer access token is required. Permission: the action the caller's "
        "role must hold, then the roles that hold it. Idempotent: whether a retry is safe and why.",
        "",
        "| # | Method | Path | Purpose | Auth | Permission (roles) | Idempotent |",
        "|---|---|---|---|---|---|---|",
    ]
    rows = 0
    for i, op in enumerate(ops, start=1):
        out.append(
            f"| {i} | {op.method.upper()} | [`{op.path}`](#{op.anchor}) | {cell(op.summary)} | "
            f"{'yes' if op.auth else 'no'} | {cell(roles_label(op.action, matrix))} | "
            f"{op.idempotent_label()} |"
        )
        rows += 1
    out.append("")

    # Per tag.
    sections = 0
    current = None
    for op in ops:
        if op.tag != current:
            current = op.tag
            title = op.tag.capitalize()
            group = [o for o in ops if o.tag == current]
            out += [f"## {title}", ""]
            blurb = TAG_BLURBS.get(op.tag)
            if blurb:
                out += [blurb, ""]
            extra = TAG_EXTRAS.get(op.tag)
            if extra:
                out += [extra.strip("\n"), ""]
            out += [
                "| Operation | Purpose |",
                "|---|---|",
            ]
            out += [f"| [`{o.heading}`](#{o.anchor}) | {cell(o.summary)} |" for o in group]
            out.append("")
        out += render_op(op, spec, renderer, matrix)
        sections += 1

    out += ["## Shared objects", ""]
    out += [
        "Objects that appear inside many answers. Operation tables link here instead of "
        "repeating their fields.",
        "",
    ]
    for name in SHARED_OBJECTS:
        target = spec.schemas[name]
        out += [f"### {name}", ""]
        if target.get("description"):
            out += [prose(target["description"]), ""]
        out += renderer.field_table({"$ref": f"#/components/schemas/{name}"}, expand_shared=True)
        out.append("")
    out += error_codes_section(spec, meta, ops)

    total = count_ops(spec.raw)
    if not (total == rows == sections == len(ops)):
        raise SystemExit(
            f"operation count mismatch: spec {total}, summary rows {rows}, sections {sections}"
        )
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).rstrip() + "\n"
    # Re-read the written summary table: every (method, path) of the spec must be a row in it.
    table = text.split("\n## All operations\n", 1)[1].split("\n## ", 1)[0]
    found = set(re.findall(r"^\| \d+ \| ([A-Z]+) \| \[`([^`]+)`\]", table, flags=re.M))
    wanted = {(o.method.upper(), o.path) for o in ops}
    if found != wanted or len(found) != total:
        missing = sorted(wanted - found)
        raise SystemExit(f"summary table rows {len(found)} != spec operations {total}: {missing}")
    return text, rows


# ---------------------------------------------------------------------------------------------
# Swagger UI page.


def build_html(spec: Spec) -> str:
    version = spec.raw.get("info", {}).get("version", "")
    embedded = copy.deepcopy(spec.raw)
    # Swagger groups by tag: the probes get the same "health" tag the Markdown uses, and every
    # tag gets its one-line description, in the Markdown's order.
    used_tags: set[str] = set()
    for item in embedded["paths"].values():
        for method, raw in item.items():
            if method in HTTP_METHODS:
                raw.setdefault("tags", ["health"])
                used_tags.update(raw["tags"])
                op_id = raw.get("operationId", "")
                if op_id in SUMMARY_OVERRIDES:
                    raw["summary"] = SUMMARY_OVERRIDES[op_id]
                for (note_op, st), body in sorted(BODY_NOTES.items()):
                    if note_op == op_id:
                        media = raw["responses"][st]["content"]["application/json"]
                        media.setdefault("example", body)
    embedded["tags"] = [
        {"name": t, "description": TAG_BLURBS.get(t, "")} for t in sorted(used_tags, key=tag_rank)
    ]
    embedded["servers"] = [
        {"url": "http://localhost:8000", "description": "Local (make dev)"},
        {"url": "http://127.0.0.1:4010", "description": "Local mock (make mock)"},
        {
            "url": "https://api.staging.{domain}",
            "description": "Staging (placeholder: set the domain after the GCP rollout)",
            "variables": {"domain": {"default": "example.com", "description": "The real domain."}},
        },
        {
            "url": "https://api.{domain}",
            "description": "Production (placeholder: set the domain after the GCP rollout)",
            "variables": {"domain": {"default": "example.com", "description": "The real domain."}},
        },
    ]
    spec_js = json.dumps(embedded, ensure_ascii=True, sort_keys=False, separators=(",", ":"))
    # Inside a <script>, "</" could end the element; JSON allows the escaped form.
    spec_js = spec_js.replace("</", "<\\/")
    cdn = f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_UI_VERSION}"
    title = f"AI Marketplace OS API v{version}"
    css_sri = SWAGGER_UI_SRI["swagger-ui.css"]
    bundle_sri = SWAGGER_UI_SRI["swagger-ui-bundle.js"]
    preset_sri = SWAGGER_UI_SRI["swagger-ui-standalone-preset.js"]
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<!-- Generated by scripts/docs/gen_api_reference.py from packages/schemas/jsonschema/openapi.json. Do not edit. -->
<link rel="stylesheet" href="{cdn}/swagger-ui.css" integrity="{css_sri}" crossorigin="anonymous">
<style>
  :root {{ --bg: #ffffff; --fg: #1f2328; --muted: #57606a; --border: #d0d7de; --note: #f6f8fa; }}
  html, body {{ margin: 0; background: var(--bg); color: var(--fg); }}
  .mkos-note {{ font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    background: var(--note); border-bottom: 1px solid var(--border); padding: 12px 16px; }}
  .mkos-note p {{ margin: 4px auto; max-width: 1400px; }}
  .mkos-note code {{ font-size: 13px; }}
  .swagger-ui .topbar {{ display: none; }}
</style>
</head>
<body>
<div class="mkos-note">
  <p><strong>{title}</strong>: the full contract, embedded in this page so it opens straight from disk (the Swagger UI scripts load from cdn.jsdelivr.net, so the browser needs internet access). Generated; do not edit.</p>
  <p>Pick a server in the <em>Servers</em> list. Staging and production are placeholders until the GCP rollout. "Try it out" needs the page to be served from an origin in the API's <code>ALLOWED_ORIGINS</code>; from a file on disk the browser blocks the calls. For live calls open the API's own Swagger UI at <code>/docs</code> (for example <code>http://localhost:8000/docs</code>; local and staging only, never production). Authorize with the <code>access_token</code> from <code>POST /api/v1/auth/login</code>.</p>
  <p>Conventions (auth, idempotency, If-Match, paging, errors, jobs) are explained in <code>API-REFERENCE.md</code> next to this file.</p>
</div>
<div id="swagger-ui"></div>
<script src="{cdn}/swagger-ui-bundle.js" integrity="{bundle_sri}" crossorigin="anonymous"></script>
<script src="{cdn}/swagger-ui-standalone-preset.js" integrity="{preset_sri}" crossorigin="anonymous"></script>
<script>
const MKOS_SPEC = {spec_js};
window.addEventListener("load", function () {{
  window.ui = SwaggerUIBundle({{
    spec: MKOS_SPEC,
    dom_id: "#swagger-ui",
    deepLinking: true,
    docExpansion: "none",
    defaultModelsExpandDepth: 0,
    displayOperationId: true,
    filter: true,
    persistAuthorization: false,
    tryItOutEnabled: false,
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
    plugins: [SwaggerUIBundle.plugins.DownloadUrl],
    layout: "StandaloneLayout"
  }});
}});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if an output is out of date")
    args = parser.parse_args(argv)

    raw_spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    apply_example_fixes(raw_spec)
    spec = Spec(raw_spec)
    matrix = load_role_matrix()
    meta = load_code_meta()
    markdown, rows = build_markdown(spec, matrix, meta)
    html = build_html(spec)

    outputs = {OUT_MD: markdown, OUT_HTML: html}
    if args.check:
        stale = [
            p
            for p, text in outputs.items()
            if not p.exists() or p.read_text(encoding="utf-8") != text
        ]
        for p in stale:
            sys.stderr.write(f"out of date: {p.relative_to(ROOT)}\n")
        return 1 if stale else 0
    for path, text in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    sys.stdout.write(
        f"gen_api_reference: {rows} operations -> "
        f"{OUT_MD.relative_to(ROOT)}, {OUT_HTML.relative_to(ROOT)}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
