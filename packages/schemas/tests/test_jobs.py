"""Jobs, seam payloads, and ModelCredentials."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from uuid import UUID

import pytest
from mkos_schemas.agent import AgentRunRecord
from mkos_schemas.enums import JobState
from mkos_schemas.jobs import (
    AttributeSpec,
    BrandChunkDTO,
    CategoryTemplate,
    GenerateListingRequest,
    GenerationContext,
    GenerationResult,
    JobAccepted,
    JobEnvelope,
    ModelCredentials,
)
from mkos_schemas.listing import CanonicalListing
from mkos_schemas.rules import RuleSet
from pydantic import SecretStr, ValidationError

_TS = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_TID = UUID("11111111-1111-1111-1111-111111111111")
_PID = UUID("22222222-2222-2222-2222-222222222222")
_LID = UUID("33333333-3333-3333-3333-333333333333")
_JID = UUID("44444444-4444-4444-4444-444444444444")


def _ruleset() -> RuleSet:
    return RuleSet.model_validate(
        {"marketplace": "amazon", "category": "apparel", "loaded_at": _TS, "rules": []}
    )


def _template(**overrides: object) -> CategoryTemplate:
    base: dict[str, object] = {"marketplace": "amazon", "category": "apparel"}
    base.update(overrides)
    return CategoryTemplate.model_validate(base)


def _context(**overrides: object) -> GenerationContext:
    base = {
        "request_id": "req-1",
        "job_id": _JID,
        "tenant_id": _TID,
        "product_id": _PID,
        "listing_id": _LID,
        "marketplace": "amazon",
        "category": "apparel",
        "product": {"title": "Product"},
        "brand_profile": {"tone": "warm"},
        "marketplace_rules": _ruleset(),
        "category_template": _template(),
    }
    base.update(overrides)
    return GenerationContext.model_validate(base)


def test_job_envelope_fields() -> None:
    for model in (JobAccepted, JobEnvelope):
        assert "status" in model.model_fields
        assert "state" not in model.model_fields
        props = model.model_json_schema().get("properties", {})
        assert "status" in props
        assert "state" not in props


def test_job_envelope_status_vocabulary() -> None:
    JobEnvelope.model_validate(
        {
            "job_id": _JID,
            "kind": "listing_generate",
            "status": "queued",
            "request_id": "r",
            "queued_at": _TS,
        }
    )
    with pytest.raises(ValidationError):
        JobEnvelope.model_validate(
            {
                "job_id": _JID,
                "kind": "listing_generate",
                "status": "timeout",
                "request_id": "r",
                "queued_at": _TS,
            }
        )


def test_job_envelope_bounds() -> None:
    JobEnvelope.model_validate(
        {
            "job_id": _JID,
            "kind": "listing_generate",
            "status": "running",
            "progress_pct": 100,
            "attempt": 3,
            "max_attempts": 3,
            "request_id": "r",
            "queued_at": _TS,
        }
    )
    with pytest.raises(ValidationError):
        JobEnvelope.model_validate(
            {
                "job_id": _JID,
                "kind": "listing_generate",
                "status": "running",
                "progress_pct": 101,
                "request_id": "r",
                "queued_at": _TS,
            }
        )


def test_generate_request_is_strict_without_tenant_id() -> None:
    with pytest.raises(ValidationError):
        GenerateListingRequest.model_validate(
            {"product_id": _PID, "marketplace": "amazon", "tenant_id": str(_TID)}
        )


def test_generate_request_user_instructions_cap() -> None:
    GenerateListingRequest.model_validate(
        {"product_id": _PID, "marketplace": "amazon", "user_instructions": "x" * 2000}
    )
    with pytest.raises(ValidationError):
        GenerateListingRequest.model_validate(
            {"product_id": _PID, "marketplace": "amazon", "user_instructions": "x" * 2001}
        )
    with pytest.raises(ValidationError):
        GenerateListingRequest.model_validate(
            {"product_id": _PID, "marketplace": "amazon", "user_instructions": "   "}
        )


def test_seam_fields_generation_context() -> None:
    names = set(GenerationContext.model_fields)
    for req in (
        "request_id",
        "job_id",
        "tenant_id",
        "product_id",
        "listing_id",
        "marketplace",
        "category",
        "product",
        "brand_profile",
        "marketplace_rules",
        "category_template",
        "previous_version",
        "user_instructions",
        "max_attempts",
        "locale",
        "review_feedback",
        "schema_version",
    ):
        assert req in names


def test_seam_fields_generation_context_types() -> None:
    ctx = _context()
    assert ctx.product_id == _PID
    assert ctx.marketplace_rules.category == "apparel"


def test_generation_context_max_attempts_capped() -> None:
    with pytest.raises(ValidationError):
        _context(max_attempts=0)
    with pytest.raises(ValidationError):
        _context(max_attempts=4)


def test_generation_context_review_feedback_caps() -> None:
    _context(review_feedback=["a" * 2000] * 5)
    with pytest.raises(ValidationError):
        _context(review_feedback=["a"] * 6)
    with pytest.raises(ValidationError):
        _context(review_feedback=["b" * 2001])


def test_seam_fields_generation_result() -> None:
    GenerationResult.model_validate(
        {
            "request_id": "r",
            "job_id": _JID,
            "terminal_state": "generated",
            "attempts_used": 3,
        }
    )
    with pytest.raises(ValidationError):
        GenerationResult.model_validate(
            {
                "request_id": "r",
                "job_id": _JID,
                "terminal_state": "approved",
                "attempts_used": 0,
            }
        )


def test_seam_fields_brand_chunk_dto() -> None:
    BrandChunkDTO.model_validate(
        {
            "id": _JID,
            "tenant_id": _TID,
            "type": "tone_rule",
            "text": "hello",
            "similarity": 0.5,
            "source_type": "brand_document",
            "source_id": _PID,
            "chunk_index": 0,
        }
    )
    with pytest.raises(ValidationError):
        BrandChunkDTO.model_validate(
            {
                "id": _JID,
                "tenant_id": _TID,
                "type": "tone_rule",
                "text": "h",
                "similarity": math.nan,
                "source_type": "brand_document",
                "source_id": _PID,
            }
        )


def test_seam_fields_category_template() -> None:
    t = _template(
        variation_themes=[["size", "color"]],
        conditional_attributes=["size"],
        unsupported_attributes=[],
        specs={"size": {"requirement": "required", "value_type": "text"}},
    )
    assert t.variation_themes[0] == ["size", "color"]


def test_category_template_for_generation_strips_adapter_fields() -> None:
    t = _template(
        specs={
            "size": {
                "requirement": "required",
                "value_type": "text",
                "marketplace_field": "SizeName",
                "value_map": {"s": "S"},
            }
        }
    )
    gen = t.for_generation()
    assert gen.specs["size"].marketplace_field is None
    assert gen.specs["size"].value_map == {}
    assert t.specs["size"].marketplace_field == "SizeName"


def test_generation_context_rejects_adapter_populated_template() -> None:
    t = _template(
        specs={
            "size": {
                "requirement": "required",
                "value_type": "text",
                "marketplace_field": "SizeName",
            }
        }
    )
    with pytest.raises(ValidationError):
        _context(category_template=t)
    assert t.specs["size"].marketplace_field == "SizeName"


def test_attribute_spec_vocabularies() -> None:
    AttributeSpec.model_validate({"requirement": "required", "value_type": "text"})
    with pytest.raises(ValidationError):
        AttributeSpec.model_validate({"requirement": "bogus", "value_type": "text"})


def test_seam_fields_model_credentials() -> None:
    assert set(ModelCredentials.model_fields) == {
        "provider",
        "api_key",
        "api_base",
        "model",
        "timeout_seconds",
    }


def test_model_credentials_secret_hidden() -> None:
    cred = ModelCredentials(
        provider="openai",
        api_key=SecretStr("sk-secret"),
        model="gpt-4",
    )
    assert "sk-secret" not in repr(cred)
    assert "sk-secret" not in str(cred)
    assert "sk-secret" not in f"{cred}"
    assert cred.model_dump(mode="json")["api_key"] == "**********"
    assert cred.model_dump(mode="json")["api_key"] == "**********"
    assert "sk-secret" not in cred.model_dump_json()
    schema = ModelCredentials.model_json_schema()
    assert schema["properties"]["api_key"].get("writeOnly") is True
    assert cred.api_key.get_secret_value() == "sk-secret"


def test_model_credentials_validation_hides_api_key_in_errors() -> None:
    with pytest.raises(ValidationError) as exc:
        ModelCredentials.model_validate({"provider": "bogus", "api_key": "sk-leak", "model": "m"})
    blob = str(exc.value) + json.dumps(exc.value.errors(), default=str)
    assert "sk-leak" not in blob


def test_model_credentials_api_base_https_only() -> None:
    ModelCredentials(
        provider="openai", api_key="k", model="m", api_base="https://api.openai.com/v1"
    )
    ModelCredentials(provider="openai", api_key="k", model="m", api_base=None)
    with pytest.raises(ValidationError):
        ModelCredentials(provider="openai", api_key="k", model="m", api_base="http://x.com")


def test_model_credentials_api_base_rejects_userinfo_and_private_hosts() -> None:
    for url in (
        "https://user:pass@host/",
        "https://127.0.0.1/",
        "https://169.254.169.254/",
        "https://169.254.1.1/",
    ):
        with pytest.raises(ValidationError):
            ModelCredentials(provider="openai", api_key="k", model="m", api_base=url)


def test_model_credentials_absent_from_result_types() -> None:
    for model in (GenerationResult, AgentRunRecord):
        schema = json.dumps(model.model_json_schema())
        assert "api_key" not in schema


def test_model_credentials_field_types() -> None:
    with pytest.raises(ValidationError):
        ModelCredentials(provider="openai", api_key="k", model="  ", timeout_seconds=0)
    ModelCredentials(provider="openai", api_key="k", model="m", timeout_seconds=600)


def test_brand_chunk_text_max_code_points() -> None:
    BrandChunkDTO.model_validate(
        {
            "id": _JID,
            "tenant_id": _TID,
            "type": "glossary",
            "text": "w" * 2000,
            "similarity": 0.0,
            "source_type": "brand_profile",
            "source_id": _PID,
        }
    )
    with pytest.raises(ValidationError):
        BrandChunkDTO.model_validate(
            {
                "id": _JID,
                "tenant_id": _TID,
                "type": "glossary",
                "text": "w" * 2001,
                "similarity": 0.0,
                "source_type": "brand_profile",
                "source_id": _PID,
            }
        )


def test_agent_block_status_vocabulary() -> None:
    from mkos_schemas.jobs import AgentBlockStatus

    AgentBlockStatus.model_validate({"agent": "content", "status": "ok"})
    with pytest.raises(ValidationError):
        AgentBlockStatus.model_validate({"agent": "content", "status": "pending"})


def test_seam_tenant_id_placement() -> None:
    assert "tenant_id" in GenerationContext.model_fields
    assert "tenant_id" in BrandChunkDTO.model_fields
    assert "tenant_id" in AgentRunRecord.model_fields
    assert "tenant_id" not in CanonicalListing.model_fields
    assert "tenant_id" not in GenerationResult.model_fields


def test_job_accepted_default_status() -> None:
    ja = JobAccepted.model_validate(
        {"job_id": _JID, "poll_url": "/jobs/1", "estimated_duration_seconds": 10}
    )
    assert ja.status is JobState.QUEUED
