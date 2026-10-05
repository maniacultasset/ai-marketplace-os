"""Seam secret deny-list (AC 17)."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from mkos_schemas.base import (
    SEAM_JSON_SCAN_MAX_ATTEMPTS,
    SEAM_REDACTED,
    SEAM_SECRET_DENY_NAMES,
    find_seam_secret_key,
    is_seam_secret_key,
    walk_seam_dicts_and_text,
)
from mkos_schemas.errors import ErrorItem
from mkos_schemas.jobs import BrandChunkDTO, CategoryTemplate, GenerationContext, ModelCredentials
from pydantic import BaseModel, ValidationError

_TID = UUID("11111111-1111-1111-1111-111111111111")


def _walk_schema_props(schema: dict[str, Any]) -> set[str]:
    names: set[str] = set()

    def visit(node: dict[str, Any]) -> None:
        for prop in node.get("properties", {}):
            names.add(prop)
        for d in node.get("$defs", {}).values():
            if isinstance(d, dict):
                visit(d)

    visit(schema)
    return names


def test_seam_secret_names_absent() -> None:
    models = (
        GenerationContext,
        BrandChunkDTO,
        CategoryTemplate,
        ErrorItem,
    )
    for model in models:
        props = _walk_schema_props(model.model_json_schema())
        bad = props & SEAM_SECRET_DENY_NAMES
        assert not bad, f"{model.__name__} exposes {bad}"
    cred_props = _walk_schema_props(ModelCredentials.model_json_schema())
    assert "api_key" in cred_props
    assert cred_props - {"api_key"} & SEAM_SECRET_DENY_NAMES == set()


def test_seam_secret_walk_rejects() -> None:
    from datetime import datetime

    from mkos_schemas.rules import RuleSet

    ts = datetime(2026, 1, 1, tzinfo=UTC)
    rs = RuleSet.model_validate(
        {"marketplace": "amazon", "category": "apparel", "loaded_at": ts, "rules": []}
    )
    tmpl = CategoryTemplate.model_validate({"marketplace": "amazon", "category": "apparel"})
    base = {
        "request_id": "r",
        "job_id": UUID("22222222-2222-2222-2222-222222222222"),
        "tenant_id": _TID,
        "product_id": UUID("33333333-3333-3333-3333-333333333333"),
        "listing_id": UUID("44444444-4444-4444-4444-444444444444"),
        "marketplace": "amazon",
        "category": "apparel",
        "marketplace_rules": rs,
        "category_template": tmpl,
    }
    with pytest.raises(ValidationError):
        GenerationContext.model_validate({**base, "product": {"api_key": "x"}, "brand_profile": {}})
    with pytest.raises(ValidationError):
        GenerationContext.model_validate({**base, "product": {}, "brand_profile": {"secret": "x"}})
    GenerationContext.model_validate({**base, "product": {"title": "ok"}, "brand_profile": {}})


def test_emit_node_event_detail_walk() -> None:
    class _Probe(BaseModel):
        detail: dict[str, Any] | None = None

    probe = _Probe(detail={"nested": {"api_key": "x"}})
    with pytest.raises(ValidationError):
        walk_seam_dicts_and_text(probe)


_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _golden_context() -> dict[str, Any]:
    raw: dict[str, Any] = json.loads(
        (_FIXTURES / "GenerationContext.json").read_text(encoding="utf-8")
    )
    return raw


def _error_item(**overrides: object) -> ErrorItem:
    raw: dict[str, object] = {
        "field": "content.title",
        "error_code": "FIELD_TOO_LONG",
        "message": "m",
        "source": "rules_engine",
        "retryable": False,
    }
    return ErrorItem.model_validate({**raw, **overrides})


# Round 1 R1-2/R1-4: case, camelCase, suffixes, lists, nested lists and JSON arrays in text.
@pytest.mark.parametrize(
    "product",
    [
        {"headers": {"Authorization": "Bearer x"}},
        {"API_KEY": "x"},
        {"apiKey": "x"},
        {"X-Api-Key": "x"},
        {"lwa_client_secret": "x"},
        {"meta": {"accessToken": "x"}},
        {"notes": ['{"api_key": "x"}']},
        {"notes": [[{"api_key": "x"}]]},
        {"notes": ({"password": "x"},)},
        {"notes": '[{"client_secret": "x"}]'},
        {"notes": '{"outer": "[{\\"refresh_token\\": 1}]"}'},
    ],
)
def test_seam_secret_walk_rejects_bypasses(product: dict[str, Any]) -> None:
    raw = _golden_context()
    raw["product"] = product
    with pytest.raises(ValidationError, match="forbidden seam key"):
        GenerationContext.model_validate(raw)


@pytest.mark.parametrize(
    "key", ["secret_sauce_flavor", "token_count", "max_tokens", "keywords", "api", "passwords_hint"]
)
def test_seam_secret_walk_keeps_legitimate_keys(key: str) -> None:
    assert not is_seam_secret_key(key)
    raw = _golden_context()
    raw["product"] = {key: "x"}
    GenerationContext.model_validate(raw)


def test_seam_secret_walk_error_never_echoes_the_secret() -> None:
    # Round 1 R1-3: the ValidationError went to logs and Sentry with the secret dict as input.
    raw = _golden_context()
    raw["product"] = {"meta": {"api_key": "sk-LEAKED123"}}
    with pytest.raises(ValidationError) as exc:
        GenerationContext.model_validate(raw)
    assert "sk-LEAKED123" not in str(exc.value)
    assert "sk-LEAKED123" not in json.dumps(exc.value.errors(include_url=False), default=str)
    assert SEAM_REDACTED in str(exc.value)


def test_error_item_redacts_secret_text_instead_of_raising() -> None:
    # Round 1 R1-3: the error path must still produce an ErrorReport (CANON.md:57).
    item = _error_item(message='{"password": "leak"}', suggestion='[{"Authorization": "x"}]')
    assert item.message == SEAM_REDACTED
    assert item.suggestion == SEAM_REDACTED
    assert "leak" not in item.model_dump_json()


def test_error_item_checks_value_submitted_before_truncating() -> None:
    # Round 1 R1-2: a 700-char secret JSON was cut to 600, stopped parsing and slipped through.
    item = _error_item(value_submitted='{"api_key": "' + "a" * 700 + '"}')
    assert item.value_submitted == SEAM_REDACTED
    item = _error_item(value_submitted={"nested": [{"client_secret": "x"}]})
    assert item.value_submitted == SEAM_REDACTED
    assert _error_item(value_submitted="b" * 700).value_submitted == "b" * 600


# Round 2 R2-1: JSON after a text prefix, a BOM-led cell and double-encoded JSON were not walked.
_R2_SECRET = "sk-LIVE-ROUND2-SECRET-VALUE"


@pytest.mark.parametrize(
    "overrides",
    [
        {"message": 'SP-API 401 body: {"access_token": "' + _R2_SECRET + '"}'},
        {"suggestion": 'retry with {"client_secret": "' + _R2_SECRET + '"}'},
        {"value_submitted": 'row 3: {"password": "' + _R2_SECRET + '"}'},
        {"value_submitted": '\ufeff{"api_key": "' + _R2_SECRET + '"}'},
        {"message": 'body: [1, {"x": [{"Authorization": "' + _R2_SECRET + '"}]}] tail'},
        {"message": 'see {not json} then {"refresh_token": "' + _R2_SECRET + '"}'},
    ],
)
def test_error_item_redacts_json_embedded_after_prefix(overrides: dict[str, object]) -> None:
    dumped = _error_item(**overrides).model_dump_json()
    assert _R2_SECRET not in dumped
    assert SEAM_REDACTED in dumped


def test_walk_decodes_double_encoded_json() -> None:
    text = json.dumps(json.dumps({"access_token": _R2_SECRET}))
    assert find_seam_secret_key(text) is not None
    assert find_seam_secret_key(json.dumps(text)) is not None
    raw = _golden_context()
    raw["product"] = {**raw["product"], "raw": text}
    with pytest.raises(ValidationError, match="forbidden seam key") as exc:
        GenerationContext.model_validate(raw)
    assert _R2_SECRET not in str(exc.value)


def test_generation_context_rejects_json_embedded_after_prefix() -> None:
    raw = _golden_context()
    raw["product"] = {**raw["product"], "notes": 'imported: {"lwa_client_secret": "x"}'}
    with pytest.raises(ValidationError, match="forbidden seam key"):
        GenerationContext.model_validate(raw)


def test_walk_keeps_text_without_secret_json() -> None:
    message = 'SP-API 400 body: {"errors": [{"code": "InvalidInput"}]} at {row} [see note]'
    assert find_seam_secret_key(message) is None
    assert _error_item(message=message).message == message
    assert find_seam_secret_key('"just a quoted string"') is None
    # Braces that cannot start a JSON value cost no scan attempt, so long template text is legal.
    templates = "{name} " * (SEAM_JSON_SCAN_MAX_ATTEMPTS * 4)
    assert find_seam_secret_key(templates) is None


def test_walk_fails_closed_when_scan_cap_is_hit() -> None:
    # Each "[1" candidate fails to decode; past the cap the rest is unscanned, so it is refused.
    text = "[1 " * SEAM_JSON_SCAN_MAX_ATTEMPTS + '{"api_key": "' + _R2_SECRET + '"}'
    assert find_seam_secret_key(text) is not None
    assert _error_item(value_submitted=text).value_submitted == SEAM_REDACTED
    # Exactly the cap with nothing left to scan is legal; one candidate more is refused.
    assert find_seam_secret_key("[1 " * SEAM_JSON_SCAN_MAX_ATTEMPTS) is None
    assert find_seam_secret_key("[1 " * (SEAM_JSON_SCAN_MAX_ATTEMPTS + 1)) is not None


def test_walk_fails_closed_on_deep_json_after_prefix() -> None:
    text = "prefix " + "[" * 100_000 + "]" * 100_000
    assert find_seam_secret_key(text) is not None


_R3_SECRET = "sk-LIVE-ROUND3-SECRET-VALUE"
_R3_INNER = json.dumps({"access_token": _R3_SECRET})


@pytest.mark.parametrize(
    "text",
    [
        "upstream body: " + json.dumps(_R3_INNER),
        json.dumps(_R3_INNER) + " (truncated)",
        "body=" + json.dumps(json.dumps({"client_secret": _R3_SECRET})),
        json.dumps(_R3_INNER)[1:-1],
        json.dumps({"a": "note " + json.dumps(_R3_INNER)}),
        "log: " + json.dumps("prefix " + json.dumps(_R3_INNER) + " tail") + " end",
    ],
)
def test_walk_decodes_escaped_json_inside_text(text: str) -> None:
    # Double-encoded JSON after a prefix, before trailing text, without its quotes, or inside a
    # JSON string value is walked like top-level JSON.
    assert find_seam_secret_key(text) is not None
    for field in ("message", "suggestion", "value_submitted"):
        dumped = _error_item(**{field: text}).model_dump_json()
        assert _R3_SECRET not in dumped, field
        assert SEAM_REDACTED in dumped
    raw = _golden_context()
    raw["product"] = {**raw["product"], "notes": text}
    with pytest.raises(ValidationError, match="forbidden seam key") as exc:
        GenerationContext.model_validate(raw)
    assert _R3_SECRET not in str(exc.value)


_FOOTNOTES = " ".join(f"Dermatologist tested [{i}]." for i in range(1, 70))
_CHECKBOXES = "\n".join(f"- [ ] tone rule {i}" for i in range(70))


@pytest.mark.parametrize("text", [_FOOTNOTES, _CHECKBOXES])
def test_walk_keeps_prose_with_many_harmless_arrays(text: str) -> None:
    # Successful decodes (``[1]``, ``[ ]``) do not spend the fail-closed budget.
    assert len(text) <= 2000
    assert find_seam_secret_key(text) is None
    chunk: dict[str, Any] = {
        "id": "22222222-2222-2222-2222-222222222222",
        "tenant_id": str(_TID),
        "type": "tone_rule",
        "text": text,
        "similarity": 0.5,
        "source_type": "brand_document",
        "source_id": "33333333-3333-3333-3333-333333333333",
    }
    assert BrandChunkDTO.model_validate(chunk).text == text
    assert _error_item(suggestion=text[:1000]).suggestion == text[:1000]
    raw = _golden_context()
    raw["product"] = {**raw["product"], "notes": text}
    GenerationContext.model_validate(raw)


def test_walk_cap_counts_failed_attempts_across_both_passes() -> None:
    # Failures in the plain pass and the unescaped-quote pass share one budget.
    text = "[1 " * (SEAM_JSON_SCAN_MAX_ATTEMPTS // 2 + 1) + '\\"'
    assert find_seam_secret_key(text) is not None
    assert find_seam_secret_key("[1 " * (SEAM_JSON_SCAN_MAX_ATTEMPTS // 2 + 1)) is None
