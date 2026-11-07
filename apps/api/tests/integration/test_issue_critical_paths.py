"""SI4 A6 (AC1; CP-D4, CP-D9): an item that claims a fix on a critical fact key is stored truthful
(`auto_fixable` false, no `fix_action`), whichever path writes it: a publish outcome or the
PATCH re-check."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, FixAction, Marketplace, RetryClass, Severity
from mkos_schemas.errors import ErrorItem, ErrorReport

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .products_support import bearer_for
from .publish_support import PubWorld, approved, pub, publish_and_run
from .review_support import OPERATOR, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world", "pub"]
CRITICAL_PATH = "attributes.fiber_composition"


def _remap_item(source: ErrorSource, **over: Any) -> ErrorItem:
    data: dict[str, Any] = {
        "field": CRITICAL_PATH,
        "error_code": ErrorCode.VALUE_NOT_ALLOWED,
        "severity": Severity.ERROR,
        "message": "The value is not accepted.",
        "source": source,
        "retryable": False,
        "retry_class": RetryClass.USER_ACTION,
        "auto_fixable": True,
        "fix_action": FixAction.REMAP_VALUE,
        "raw_code": "90220",
    }
    data.update(over)
    return ErrorItem.model_validate(data)


def _issue_rows(conn: Any, listing_id: Any) -> Any:
    return conn.execute(
        "SELECT field_path, source, auto_fixable, fix_action FROM listing_issues "
        "WHERE listing_id = %s ORDER BY created_at, id",
        [listing_id],
    ).fetchall()


async def test_publish_outcome_stores_a_critical_remap_unfixable(
    api: httpx.AsyncClient, pub: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": ("90220", "refused")})

    def item(field: str) -> ErrorItem:
        return _remap_item(ErrorSource.AMAZON_ADAPTER, marketplace=Marketplace.AMAZON)

    monkeypatch.setattr(pub.fake, "_item", item)
    await publish_and_run(api, pub, made)
    rows = [r for r in _issue_rows(pub.conn, made.listing_id) if r[0] == CRITICAL_PATH]
    assert rows == [(CRITICAL_PATH, "amazon_adapter", False, None)]


async def test_patch_recheck_stores_a_critical_remap_unfixable(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import listings

    made = await generated(gen_world)
    report = ErrorReport(status="failed", errors=[_remap_item(ErrorSource.RULES_ENGINE)])
    monkeypatch.setattr(listings, "recheck", lambda *_a, **_k: report)
    headers = {**bearer_for(gen_world, OPERATOR), "If-Match": f'"{made.version_id}"'}
    response = await api.patch(
        f"/api/v1/listings/{made.listing_id}/content",
        headers=headers,
        json={
            "content": {
                "title": "Cotton Kurta Set for Women",
                "bullets": ["Soft cotton fabric"],
                "description": "A cotton kurta set with a straight fit.",
            }
        },
    )
    assert response.status_code == 200, response.text
    rows = _issue_rows(gen_world.conn, made.listing_id)
    assert (CRITICAL_PATH, "rules_engine", False, None) in rows


async def test_a_non_critical_remap_keeps_its_fix_action(
    api: httpx.AsyncClient, pub: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the same item on a non-critical key is stored as the item says."""
    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": ("90220", "refused")})

    def item(field: str) -> ErrorItem:
        return _remap_item(
            ErrorSource.AMAZON_ADAPTER, field="attributes.fit", marketplace=Marketplace.AMAZON
        )

    monkeypatch.setattr(pub.fake, "_item", item)
    await publish_and_run(api, pub, made)
    rows = [r for r in _issue_rows(pub.conn, made.listing_id) if r[0] == "attributes.fit"]
    assert rows == [("attributes.fit", "amazon_adapter", True, "remap_value")]
