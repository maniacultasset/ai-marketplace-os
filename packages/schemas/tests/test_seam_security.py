"""Seam secret deny-list (AC 17)."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from mkos_schemas.base import (
    SEAM_REDACTED,
    SEAM_SECRET_DENY_NAMES,
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
