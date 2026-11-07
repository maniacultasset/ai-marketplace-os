"""SI2 T16 (AC7, D5): the correction loop's Rules call judges the seller's factual values."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

LOCAL_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os"
NOW = datetime(2026, 11, 7, tzinfo=UTC)


def _deps(facts: Any) -> Any:
    from app.core.config import Settings
    from app.db.seed_templates import seed_templates
    from app.services.pipeline_deps import BackendPipelineDeps, _ModelConfig
    from app.services.rules_loader import LoadedRules
    from mkos_schemas.enums import Marketplace
    from mkos_schemas.rules import RuleSet

    (seed,) = [s for s in seed_templates() if s.template.category == "ethnic_wear_kurta_sets"]
    cfg = Settings(_env_file=None, app_env="local", database_url=LOCAL_URL)
    ruleset = RuleSet(marketplace="amazon", category="ethnic_wear_kurta_sets", loaded_at=NOW)
    return BackendPipelineDeps(
        tenant_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        user_id=None,
        marketplace=Marketplace.AMAZON,
        rules=LoadedRules(ruleset=ruleset, template=seed.template),
        product=None,
        image_reports={},
        embedder=None,
        models=_ModelConfig.from_settings(cfg),
        facts=facts,
    )


def _listing() -> Any:
    from tests.adapters.contract import sample_listing

    body = sample_listing()
    return body.model_copy(
        update={
            "category": "ethnic_wear_kurta_sets",
            "attributes": {"color": "navy blue"},  # the AI left fabric_type out
            "confidence_score": 0.9,
        }
    )


def _facts(listing: Any) -> Any:
    from app.core.resolved_facts import ProductFacts, ResolvedFact

    return ProductFacts(
        product_id=listing.product_id,
        sku="SKU-1",
        level="standalone",
        attributes={
            "fabric_type": ResolvedFact(key="fabric_type", value="Cotton Lycra", source="seller"),
            "color": ResolvedFact(key="color", value="navy blue", source="seller"),
        },
    )


def _missing(report: Any) -> set[str]:
    return {e.field for e in report.errors if e.error_code.value == "MISSING_ATTRIBUTE"}


async def test_seller_fact_fills_what_the_ai_left_out() -> None:
    listing = _listing()
    deps = _deps(_facts(listing))
    report = await deps.validate_rules(listing)
    assert "attributes.fabric_type" not in _missing(report)
    assert len(deps.rules_calls) == 1


async def test_without_facts_the_old_behaviour_holds() -> None:
    listing = _listing()
    report = await _deps(None).validate_rules(listing)
    assert "attributes.fabric_type" in _missing(report)
