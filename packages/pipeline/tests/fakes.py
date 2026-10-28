from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from mkos_schemas.codes import ErrorCode
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.images import ImageReport
from mkos_schemas.jobs import GenerationContext
from mkos_schemas.listing import CanonicalListing, CategorySlug
from mkos_schemas.pipeline import BrandChunkDTO, ModelCredentials
from mkos_schemas.rules import AttributeSpec, CategoryTemplate, Rule, RuleSet
from pydantic import SecretStr

from mkos_pipeline.llm.client import LLMResponse

TENANT = UUID("11111111-1111-1111-1111-111111111111")
PRODUCT = UUID("4e8a1b30-77cc-41d9-8f02-5b9c1de3a004")
LISTING = UUID("9c1d2f6a-3b84-4f21-9a55-0c6f8b2e77d1")
JOB = UUID("44444444-4444-4444-4444-444444444444")

SEEDED_BANNED = ["sale", "discount", "cheapest"]


def seeded_rules(marketplace: str, category: CategorySlug, title_max: int) -> RuleSet:
    return RuleSet(
        marketplace=marketplace,  # type: ignore[arg-type]
        category=category,
        rules_version=1,
        loaded_at=datetime(2026, 9, 22, 10, 29, tzinfo=UTC),
        rules=[
            Rule(
                field="content.title",
                rule_type="max_length",
                rule_def={"max_length": title_max},
                severity="error",
            ),
            Rule(
                field="content.title",
                rule_type="min_length",
                rule_def={"min_length": 1},
            ),
            Rule(
                field="*",
                rule_type="banned_terms",
                rule_def={"terms": SEEDED_BANNED},
            ),
            Rule(
                field="attributes.color",
                rule_type="required_attribute",
                rule_def={"attribute": "color"},
            ),
        ],
    )


def seeded_template(marketplace: str, category: CategorySlug) -> CategoryTemplate:
    required = ["color", "fabric_type"]
    if category == "home_decor_lamps":
        required = ["color", "product_dimensions"]
    return CategoryTemplate(
        marketplace=marketplace,  # type: ignore[arg-type]
        category=category,
        required_attributes=required,
        optional_attributes=["sleeve_length", "pattern", "fit"],
        specs={
            "color": AttributeSpec(requirement="required", allowed_values=["indigo", "navy", "blue", "white"]),
            "fabric_type": AttributeSpec(requirement="required", allowed_values=["cotton", "linen", "silk"]),
            "sleeve_length": AttributeSpec(
                requirement="optional",
                allowed_values=["short", "three_quarter", "long"],
            ),
        },
    )


def make_context(
    *,
    category: CategorySlug = "ethnic_wear_kurta_sets",
    marketplace: str = "amazon",
    product: dict[str, Any] | None = None,
    locale: str = "en-IN",
    max_attempts: int = 3,
    user_instructions: str | None = None,
) -> GenerationContext:
    title_max = 150 if marketplace == "flipkart" else 200
    default_product = {
        "product_id": str(PRODUCT),
        "sku": "KURTA-001",
        "identity": {"title": "Cotton Kurta Set", "brand_name": "Acme", "gender": "women"},
        "classification": {"category": category},
        "attributes": {"color": "indigo", "fabric_type": "cotton", "sleeve_length": "three_quarter"},
    }
    return GenerationContext(
        request_id="req-golden-1",
        job_id=JOB,
        tenant_id=TENANT,
        product_id=PRODUCT,
        listing_id=LISTING,
        marketplace=marketplace,  # type: ignore[arg-type]
        category=category,
        product=product or default_product,
        brand_profile={"tone": "warm", "brand_names": ["Acme"]},
        marketplace_rules=seeded_rules(marketplace, category, title_max),
        category_template=seeded_template(marketplace, category),
        user_instructions=user_instructions,
        max_attempts=max_attempts,
        locale=locale,  # type: ignore[arg-type]
    )


def evaluate_listing(listing: CanonicalListing, ctx: GenerationContext) -> ErrorReport:
    errors: list[ErrorItem] = []
    title = listing.content.title
    for rule in ctx.marketplace_rules.rules:
        if not rule.enabled:
            continue
        if rule.rule_type == "max_length" and rule.field == "content.title":
            limit = int(rule.rule_def["max_length"])
            if len(title) > limit:
                errors.append(
                    ErrorItem(
                        field="content.title",
                        error_code=ErrorCode.FIELD_TOO_LONG,
                        message=f"Title exceeds {limit} characters.",
                        retryable=True,
                        source="rules_engine",
                        suggestion=f"Shorten the title to {limit} characters or fewer.",
                    )
                )
        if rule.rule_type == "banned_terms":
            terms = [str(term).casefold() for term in rule.rule_def.get("terms", [])]
            blob = " ".join(
                [title, listing.content.description, *listing.content.bullets, *listing.keywords.search_terms]
            ).casefold()
            for term in terms:
                if term in blob:
                    errors.append(
                        ErrorItem(
                            field="content.title",
                            error_code=ErrorCode.BANNED_TERM,
                            message=f"Banned term {term!r} found.",
                            retryable=True,
                            source="rules_engine",
                            suggestion="Remove the banned term.",
                        )
                    )
        if rule.rule_type == "required_attribute":
            key = str(rule.rule_def.get("attribute") or rule.field.rsplit(".", 1)[-1])
            if key not in listing.attributes:
                errors.append(
                    ErrorItem(
                        field=f"attributes.{key}",
                        error_code=ErrorCode.MISSING_ATTRIBUTE,
                        message=f"Required attribute {key} is missing.",
                        retryable=False,
                        source="rules_engine",
                    )
                )
    for key in ctx.category_template.required_attributes:
        if key not in listing.attributes:
            errors.append(
                ErrorItem(
                    field=f"attributes.{key}",
                    error_code=ErrorCode.MISSING_ATTRIBUTE,
                    message=f"Required attribute {key} is missing.",
                    retryable=False,
                    source="rules_engine",
                )
            )
    # de-dupe missing attributes
    seen: set[str] = set()
    unique: list[ErrorItem] = []
    for item in errors:
        marker = f"{item.field}:{item.error_code}"
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(item)
    return ErrorReport(
        status="passed" if not unique else "failed",
        errors=unique,
        error_code=None if not unique else unique[0].error_code,
    )


class FakeDeps:
    def __init__(self, ctx: GenerationContext, *, fail_on_fourth: bool = True) -> None:
        self.ctx = ctx
        self.events: list[tuple[str, str]] = []
        self.rule_calls = 0
        self.fail_on_fourth = fail_on_fourth

    async def retrieve_brand_chunks(
        self,
        tenant_id: UUID,
        query: str,
        k: int = 8,
        types: list[str] | None = None,
    ) -> list[BrandChunkDTO]:
        assert tenant_id == self.ctx.tenant_id
        assert 1 <= len(query) <= 2000
        chunk_type = (types or ["tone_rule"])[0]
        return [
            BrandChunkDTO(
                id=uuid4(),
                tenant_id=tenant_id,
                type=chunk_type,  # type: ignore[arg-type]
                text="Speak warmly. Do not shout.",
                similarity=0.9,
                source_type="brand_document",
                source_id=uuid4(),
                chunk_index=0,
            )
        ][:k]

    async def validate_rules(self, listing: CanonicalListing) -> ErrorReport:
        self.rule_calls += 1
        if self.fail_on_fourth and self.rule_calls > 3:
            return ErrorReport(
                status="failed",
                error_code=ErrorCode.INTERNAL_ERROR,
                errors=[
                    ErrorItem(
                        field="*",
                        error_code=ErrorCode.INTERNAL_ERROR,
                        message="Fourth validate_rules call is refused.",
                        retryable=False,
                        source="system",
                    )
                ],
            )
        return evaluate_listing(listing, self.ctx)

    async def validate_images(self, listing: CanonicalListing) -> list[ImageReport]:
        del listing
        return []

    async def get_model_credentials(self, purpose: str) -> ModelCredentials:
        if purpose == "embedding":
            raise RuntimeError("embedding credentials are backend-owned")
        return ModelCredentials(
            provider="deepseek",
            api_key=SecretStr("sk-test-secret"),
            model="deepseek-chat",
            timeout_seconds=60,
        )

    async def emit_node_event(self, node: str, status: str, detail: dict[str, Any] | None = None) -> None:
        known = {
            "load_context",
            "retrieve_brand",
            "content_agent",
            "keyword_agent",
            "attribute_agent",
            "image_agent",
            "merge_outputs",
            "validate_schema",
            "validate_rules",
            "validate_vision",
            "correct",
            "quality_check",
        }
        if node not in known or status not in {"started", "ok", "failed"}:
            raise ValueError(f"unknown node or status: {node} {status}")
        self.events.append((node, status))


def _data_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    user = payload["messages"][1]["content"]
    start = user.rfind("<data>")
    end = user.find("</data>", start)
    if start < 0 or end < 0:
        return {}
    body = user[start + len("<data>") : end].strip()
    if not body:
        return {}
    return json.loads(body)


class ProductAwareTransport:
    """Builds valid JSON from the product facts in the data block."""

    def __init__(self, *, long_title: bool = False, banned_keyword: bool = False) -> None:
        self.long_title = long_title
        self.banned_keyword = banned_keyword
        self.calls = 0

    async def __call__(self, payload: dict[str, Any], timeout: float) -> LLMResponse:
        del timeout
        self.calls += 1
        user = payload["messages"][1]["content"]
        data = _data_from_payload(payload)
        identity = data.get("identity") or {}
        attrs = data.get("attributes") or data.get("product_attributes") or {}
        title = str(identity.get("title") or "Cotton Kurta Set")
        color = str(attrs.get("color") or attrs.get("color_name") or "indigo")
        fabric = str(attrs.get("fabric_type") or attrs.get("fabric") or "cotton")
        if self.long_title and (
            "listing copy" in user.lower() or "rejected listing field" in user.lower()
        ):
            title = ("Organic Cotton Kurta Set for Women Indigo Handloom " * 8)[:221]
        if "listing copy" in user.lower() or "rejected listing field" in user:
            body = {
                "title": title,
                "bullets": [
                    f"Made from {fabric} for all-day wear",
                    f"Colour {color} with a relaxed silhouette",
                    "Paired kurta and bottom as a set",
                ],
                "description": f"The {title} is tailored in {fabric} and offered in {color}.",
                "claims": [{"text": f"Made from {fabric}", "fact_keys": ["attributes.fabric_type"]}],
            }
        elif "search keywords" in user.lower() or "rejected keyword" in user:
            terms = [f"{fabric} kurta", f"{color} kurta set", "kurta set women"]
            if self.banned_keyword:
                terms.append("cheapest kurta")
            body = {"search_terms": terms, "hidden_terms": ["cotton kurta set", "indigo kurti"]}
        else:
            picks = []
            for key, value in attrs.items():
                picks.append(
                    {
                        "key": "color" if key == "color_name" else key,
                        "value": str(value),
                        "source_fact": f"attributes.{key}",
                    }
                )
            if data.get("candidates"):
                picks = data["candidates"]
            body = {"attributes": picks}
        content = json.dumps(body)
        return LLMResponse(
            content=content,
            status_code=200,
            input_tokens=100,
            output_tokens=50,
            cached_input_tokens=0,
        )
