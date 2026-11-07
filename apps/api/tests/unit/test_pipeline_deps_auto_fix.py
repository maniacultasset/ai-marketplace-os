"""SI4 A8 (AC5, AC7): the deps' Rules Engine port runs the fix pass on a copy and reports the fixed
copy's verdict, so the pipeline spends no correction attempt on a fixable item; the scope checks,
the fail-closed path and the three-call cap are unchanged."""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import pytest
from mkos_schemas.codes import ErrorCode
from tests.rules_engine_builders import listing, rule, ruleset

LOCAL_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os"
UNIT = "Cotton Kurta Set for Women with Straight Fit and Round Neck "
TITLE_212 = (UNIT * 5)[:211] + "."
TITLE_260 = (UNIT * 5)[:259] + "."
TITLE_RULE = UUID("51400000-0000-0000-0000-00000000c001")
TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000cc")


def _deps(*rules: Any, critical: frozenset[str] = frozenset()) -> Any:
    from app.core.config import Settings
    from app.services.pipeline_deps import BackendPipelineDeps, _ModelConfig
    from app.services.rules_loader import LoadedRules
    from mkos_schemas.enums import Marketplace

    cfg = Settings(_env_file=None, app_env="local", database_url=LOCAL_URL)
    title = rule({"type": "max_length", "limit": 200}, id=TITLE_RULE, field="content.title")
    return BackendPipelineDeps(
        tenant_id=TENANT,
        job_id=uuid.uuid4(),
        user_id=None,
        marketplace=Marketplace.AMAZON,
        rules=LoadedRules(ruleset=ruleset(*(rules or (title,))), template=None),
        product=None,
        image_reports={},
        embedder=None,
        models=_ModelConfig.from_settings(cfg),
        critical_keys=critical,
    )


def _codes(report: Any) -> list[ErrorCode]:
    return [e.error_code for e in report.errors]


async def test_a_fixable_title_reports_clean() -> None:
    deps = _deps()
    report = await deps.validate_rules(listing(content={"title": TITLE_212}))
    assert ErrorCode.FIELD_TOO_LONG not in _codes(report)
    assert report.passed
    assert deps.rules_calls[0].report == report


async def test_a_title_past_the_share_still_fails() -> None:
    deps = _deps()
    report = await deps.validate_rules(listing(content={"title": TITLE_260}))
    assert _codes(report) == [ErrorCode.FIELD_TOO_LONG]


async def test_the_pipeline_listing_is_never_changed() -> None:
    deps = _deps()
    item = listing(content={"title": TITLE_212})
    await deps.validate_rules(item)
    assert item.content.title == TITLE_212


async def test_a_foreign_tenant_rule_fails_closed() -> None:
    foreign = rule(
        {"type": "banned_terms", "terms": ["x"]}, tenant_id=uuid.uuid4(), field="content.title"
    )
    deps = _deps(foreign)
    report = await deps.validate_rules(listing(content={"title": TITLE_212}))
    assert _codes(report) == [ErrorCode.INTERNAL_ERROR]


async def test_an_exception_inside_the_fix_pass_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import pipeline_deps

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("fixer bug")

    monkeypatch.setattr(pipeline_deps, "auto_fix", boom)
    report = await _deps().validate_rules(listing(content={"title": TITLE_212}))
    assert _codes(report) == [ErrorCode.INTERNAL_ERROR]


async def test_the_fourth_call_still_hits_the_cap() -> None:
    deps = _deps()
    item = listing(content={"title": TITLE_212})
    for _ in range(3):
        assert (await deps.validate_rules(item)).passed
    assert _codes(await deps.validate_rules(item)) == [ErrorCode.INTERNAL_ERROR]
    assert len(deps.rules_calls) == 3


def test_critical_keys_are_required() -> None:
    from app.services.pipeline_deps import BackendPipelineDeps

    with pytest.raises(TypeError):
        BackendPipelineDeps(  # type: ignore[call-arg]
            tenant_id=TENANT,
            job_id=uuid.uuid4(),
            user_id=None,
            marketplace="amazon",  # type: ignore[arg-type]
            rules=None,  # type: ignore[arg-type]
            product=None,
            image_reports={},
            embedder=None,
            models=None,  # type: ignore[arg-type]
        )
