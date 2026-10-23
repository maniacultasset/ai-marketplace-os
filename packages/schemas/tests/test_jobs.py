"""Jobs, seam payloads, and ModelCredentials."""

from __future__ import annotations

import base64
import json
import math
from datetime import UTC, datetime
from uuid import UUID

import pytest
from mkos_schemas.agent import AgentRunRecord
from mkos_schemas.enums import JobState
from mkos_schemas.jobs import (
    MAX_CONTEXT_IMAGE_B64_CHARS,
    MAX_CONTEXT_IMAGES,
    MAX_RESULT_CHANGES,
    MAX_RESULT_QUESTIONS,
    AttributeSpec,
    BrandChunkDTO,
    CategoryTemplate,
    ContextImage,
    FactConflict,
    FieldChange,
    GenerateListingRequest,
    GenerationContext,
    GenerationResult,
    JobAccepted,
    JobEnvelope,
    ModelCredentials,
    Provenance,
    Question,
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


def test_model_credentials_is_a_wire_model() -> None:
    # Round 1 R1-11: CANON.md:122 lists ModelCredentials among the WireModel seam types, so an
    # additive field from a newer backend is dropped by a pinned AI package, not rejected.
    from mkos_schemas.base import StrictModel, WireModel

    assert issubclass(ModelCredentials, WireModel)
    assert not issubclass(ModelCredentials, StrictModel)
    assert ModelCredentials.model_config.get("hide_input_in_errors") is True
    cred = ModelCredentials.model_validate(
        {"provider": "openai", "api_key": "k", "model": "m", "org_id": "o"}
    )
    assert "org_id" not in cred.model_dump()
    assert ModelCredentials.model_json_schema().get("additionalProperties") is not False
    with pytest.raises(ValidationError):
        ModelCredentials.model_validate({"provider": "openai", "api_key": "  ", "model": "m"})


# ---- 1.2.0: GenerationResult.questions and .changes (assignment §10.1, D1-20, D1-34) ----


def _result(**overrides: object) -> GenerationResult:
    base: dict[str, object] = {"request_id": "r", "job_id": _JID, "terminal_state": "needs_review"}
    base.update(overrides)
    return GenerationResult.model_validate(base)


def _question(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "kind": "missing_fact",
        "target_path": "facts.attributes.fiber_composition",
        "question": "What is the fibre composition of this kurta?",
        "reason": "Amazon needs the fibre composition for apparel.",
        "suggestions": [],
        "evidence": [],
    }
    base.update(overrides)
    return base


def _change(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "field": "attributes.color",
        "before": "navy",
        "after": "indigo",
        "reason": "The seller answered the colour question.",
    }
    base.update(overrides)
    return base


def test_result_questions_and_changes_default_empty() -> None:
    result = _result()
    assert result.questions == [] and result.changes == []
    dumped = result.model_dump(mode="json")
    assert dumped["questions"] == [] and dumped["changes"] == []


def test_result_questions_and_changes_round_trip() -> None:
    result = _result(
        questions=[
            _question(),
            _question(
                kind="conflicting_facts",
                target_path="facts.attributes.color",
                suggestions=["navy", "indigo"],
                evidence=["import_row:42", "image:" + str(_PID)],
            ),
        ],
        changes=[_change(), _change(field="content.bullets[1]", before=None, after="New")],
    )
    again = GenerationResult.model_validate_json(result.model_dump_json())
    assert again == result
    assert again.questions[1].suggestions == ["navy", "indigo"]
    assert again.changes[1].before is None


def test_question_kind_is_the_three_fact_kinds() -> None:
    for kind in ("missing_fact", "conflicting_facts", "unverified_declaration"):
        assert Question.model_validate(_question(kind=kind)).kind == kind
    for kind in ("approval_or_evidence", "image_replacement", "low_confidence_category"):
        with pytest.raises(ValidationError):
            Question.model_validate(_question(kind=kind))


@pytest.mark.parametrize("path", ["facts.attributes.fiber_composition", "facts.mrp", "brand.tone"])
def test_question_target_path_matches_the_task_check(path: str) -> None:
    assert Question.model_validate(_question(target_path=path)).target_path == path


@pytest.mark.parametrize(
    "path",
    [
        "attributes.color",
        "facts",
        "facts.a.b.c",
        "facts.Attributes",
        "facts..x",
        "facts.x ",
        # Every question kind is a human_tasks fact kind, and ck_human_tasks_fact_target refuses
        # an account.* target for those, so the insert would fail.
        "account.settings",
        "account.amazon.permissions",
    ],
)
def test_question_target_path_refuses_other_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        Question.model_validate(_question(target_path=path))


def test_question_text_caps() -> None:
    Question.model_validate(_question(question="a" * 5, reason="b" * 5))
    Question.model_validate(_question(question="a" * 300, reason="b" * 500))
    for bad in ({"question": "a" * 4}, {"question": "a" * 301}, {"reason": "b" * 4}):
        with pytest.raises(ValidationError):
            Question.model_validate(_question(**bad))
    with pytest.raises(ValidationError):
        Question.model_validate(_question(reason="b" * 501))


def test_question_suggestion_caps() -> None:
    Question.model_validate(_question(suggestions=["s" * 120] * 6))
    with pytest.raises(ValidationError):
        Question.model_validate(_question(suggestions=["s"] * 7))
    with pytest.raises(ValidationError):
        Question.model_validate(_question(suggestions=["s" * 121]))
    for blank in ("", "   "):
        with pytest.raises(ValidationError):
            Question.model_validate(_question(suggestions=[blank]))


def test_question_evidence_caps() -> None:
    ref = "agent_run:" + str(_JID)
    assert len(Question.model_validate(_question(evidence=[ref] * 20)).evidence) == 20
    for bad in ([ref] * 21, [""], ["  "], ["e" * 129]):
        with pytest.raises(ValidationError):
            Question.model_validate(_question(evidence=bad))


def test_result_questions_and_changes_are_capped() -> None:
    assert len(_result(questions=[_question()] * MAX_RESULT_QUESTIONS).questions) == 20
    with pytest.raises(ValidationError):
        _result(questions=[_question()] * (MAX_RESULT_QUESTIONS + 1))
    assert len(_result(changes=[_change()] * MAX_RESULT_CHANGES).changes) == MAX_RESULT_CHANGES
    with pytest.raises(ValidationError):
        _result(changes=[_change()] * (MAX_RESULT_CHANGES + 1))


@pytest.mark.parametrize("value", ["text", 3, 2.5, True, ["a", "b"], None])
def test_field_change_values_are_attribute_values(value: object) -> None:
    change = FieldChange.model_validate(_change(before=value, after=value))
    assert change.before == value and change.after == value


@pytest.mark.parametrize(
    "field", ["content.title", "content.bullets[3]", "attributes.color", "keywords.hidden_terms"]
)
def test_field_change_field_is_a_field_path(field: str) -> None:
    assert FieldChange.model_validate(_change(field=field)).field == field


@pytest.mark.parametrize(
    "field", ["", "title", "attributes.Color", "content.bullets[x]", "a" * 200]
)
def test_field_change_refuses_other_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        FieldChange.model_validate(_change(field=field))


def test_field_change_refuses_objects_and_long_reasons() -> None:
    with pytest.raises(ValidationError):
        FieldChange.model_validate(_change(after={"nested": "object"}))
    FieldChange.model_validate(_change(reason="r" * 300))
    with pytest.raises(ValidationError):
        FieldChange.model_validate(_change(reason="r" * 301))


# ---- 1.2.0: GenerationContext.product_provenance, .conflicts and .answered_keys (§10.1) ----


def _conflict(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "key": "attributes.color",
        "values": [
            {"source": "import", "value": "navy", "evidence_ref": "import_row:42"},
            {"source": "label", "value": "indigo", "evidence_ref": "image:" + str(_PID)},
        ],
    }
    base.update(overrides)
    return base


def test_context_new_fields_default_empty() -> None:
    ctx = _context()
    assert ctx.product_provenance == {}
    assert ctx.conflicts == []
    assert ctx.answered_keys == []


def test_context_new_fields_round_trip() -> None:
    ctx = _context(
        product_provenance={
            "attributes.color": {"source": "seller", "confidence": None, "confirmed": True},
            "attributes.neckline": {"source": "ai", "confidence": 0.82, "confirmed": False},
        },
        conflicts=[_conflict()],
        answered_keys=["attributes.fiber_composition"],
    )
    again = GenerationContext.model_validate_json(ctx.model_dump_json())
    assert again == ctx
    assert again.product_provenance["attributes.neckline"].confidence == 0.82
    assert again.conflicts[0].values[1].value == "indigo"
    assert again.answered_keys == ["attributes.fiber_composition"]


def test_provenance_shape() -> None:
    assert set(Provenance.model_fields) == {"source", "confidence", "confirmed"}
    for bad in (-0.01, 1.01, float("nan")):
        with pytest.raises(ValidationError):
            Provenance.model_validate({"source": "ai", "confidence": bad, "confirmed": False})


def test_conflict_needs_two_observed_values() -> None:
    raw = _conflict()
    values = raw["values"]
    assert isinstance(values, list)
    with pytest.raises(ValidationError):
        FactConflict.model_validate({**raw, "values": values[:1]})
    with pytest.raises(ValidationError):
        FactConflict.model_validate(
            {**raw, "values": [{**values[0], "value": {"nested": "object"}}, values[1]]}
        )


def test_context_from_1_1_0_still_parses() -> None:
    raw = json.loads(_context().model_dump_json())
    for key in ("product_provenance", "conflicts", "answered_keys", "images"):
        del raw[key]
    raw["schema_version"] = "1.1.0"
    ctx = GenerationContext.model_validate(raw)
    assert ctx.schema_version == "1.1.0" and ctx.conflicts == []


# ---- 1.2.0: GenerationContext.images (assignment §10.1 ContextImage; plan C11: 6 + label) ----

_JPEG = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9").decode("ascii")


def _image(n: int = 1, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "image_id": UUID(int=n),
        "slot": "main",
        "colourway_id": None,
        "width": 1024,
        "height": 768,
        "data_b64": _JPEG,
    }
    base.update(overrides)
    return base


def test_context_images_default_empty() -> None:
    assert _context().images == []
    assert _context().model_dump(mode="json")["images"] == []


def test_context_image_shape() -> None:
    assert set(ContextImage.model_fields) == {
        "image_id",
        "slot",
        "colourway_id",
        "width",
        "height",
        "data_b64",
    }


def test_context_images_round_trip_with_the_label_slot() -> None:
    ctx = _context(
        images=[
            _image(1),
            _image(2, slot="packaging_label", colourway_id=_PID, width=600, height=1024),
        ]
    )
    again = GenerationContext.model_validate_json(ctx.model_dump_json())
    assert again == ctx
    assert again.images[1].slot == "packaging_label"
    assert again.images[1].colourway_id == _PID


def test_context_images_cap_is_six_products_and_the_label() -> None:
    assert MAX_CONTEXT_IMAGES == 7
    seven = [_image(n) for n in range(1, 7)] + [_image(7, slot="packaging_label")]
    assert len(_context(images=seven).images) == 7
    with pytest.raises(ValidationError):
        _context(images=[*seven, _image(8)])


def test_context_images_refuse_a_repeated_image() -> None:
    with pytest.raises(ValidationError):
        _context(images=[_image(1), _image(1, slot="secondary")])


@pytest.mark.parametrize(
    "bad",
    [
        {"width": 0},
        {"height": 0},
        {"width": 1025},
        {"height": 1025},
        {"slot": "hero"},
        {"data_b64": ""},
        {"data_b64": "not base64!"},
        {"data_b64": "abc"},
        {"data_b64": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii")},
        {"data_b64": "A" * (MAX_CONTEXT_IMAGE_B64_CHARS + 4)},
    ],
)
def test_context_image_refuses_bad_values(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ContextImage.model_validate({**_image(), **bad})


def test_context_image_has_no_url_field() -> None:
    img = ContextImage.model_validate(_image(signed_url="https://example.com/x.jpg"))
    assert "signed_url" not in img.model_dump()


def test_context_image_bytes_stay_out_of_repr_and_errors() -> None:
    img = ContextImage.model_validate(_image())
    assert _JPEG not in repr(img) and _JPEG not in str(img)
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096).decode("ascii")
    with pytest.raises(ValidationError) as err:
        ContextImage.model_validate(_image(data_b64=png))
    assert png not in str(err.value)


def test_context_image_bytes_stay_out_of_context_errors() -> None:
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096).decode("ascii")
    with pytest.raises(ValidationError) as err:
        _context(images=[_image(data_b64=png)])
    assert png[:64] not in str(err.value)
