"""Seam secret deny-list (AC 17)."""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError

from mkos_schemas.base import SEAM_SECRET_DENY_NAMES, walk_seam_dicts_and_text
from mkos_schemas.errors import ErrorItem
from mkos_schemas.jobs import BrandChunkDTO, CategoryTemplate, GenerationContext, ModelCredentials

_TID = UUID("11111111-1111-1111-1111-111111111111")


def _walk_schema_props(schema: dict) -> set[str]:
    names: set[str] = set()

    def visit(node: dict) -> None:
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
    from datetime import datetime, timezone

    from mkos_schemas.jobs import GenerationContext as GC
    from mkos_schemas.rules import RuleSet

    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rs = RuleSet.model_validate(
        dict(marketplace="amazon", category="apparel", loaded_at=ts, rules=[])
    )
    tmpl = CategoryTemplate.model_validate(dict(marketplace="amazon", category="apparel"))
    base = dict(
        request_id="r",
        job_id=UUID("22222222-2222-2222-2222-222222222222"),
        tenant_id=_TID,
        product_id=UUID("33333333-3333-3333-3333-333333333333"),
        listing_id=UUID("44444444-4444-4444-4444-444444444444"),
        marketplace="amazon",
        category="apparel",
        marketplace_rules=rs,
        category_template=tmpl,
    )
    with pytest.raises(ValidationError):
        GC.model_validate({**base, "product": {"api_key": "x"}, "brand_profile": {}})
    with pytest.raises(ValidationError):
        GC.model_validate({**base, "product": {}, "brand_profile": {"secret": "x"}})
    with pytest.raises(ValidationError):
        ErrorItem.model_validate(
            dict(
                field="content.title",
                error_code="FIELD_TOO_LONG",
                message='{"password": "leak"}',
                source="rules_engine",
                retryable=False,
            )
        )
    GC.model_validate({**base, "product": {"title": "ok"}, "brand_profile": {}})


def test_emit_node_event_detail_walk() -> None:
    class _Probe(BaseModel):
        detail: dict | None = None

    probe = _Probe(detail={"nested": {"api_key": "x"}})
    with pytest.raises(ValidationError):
        walk_seam_dicts_and_text(probe)
