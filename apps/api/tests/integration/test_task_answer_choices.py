"""SI9 T12 (AC14; CP-D23, CP-D25): Inbox answer choices come from the listing template's accepted
values, in the template's own spelling, so a valid answer can never fail the Rules check's
`VALUE_NOT_ALLOWED`."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from app.services import photo_facts as pf
from mkos_schemas.jobs import CategoryTemplate
from tests.unit.test_photo_targets import KURTA

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, set_status
from .photo_support import PhotoWorld, generate, photo_world, regenerable
from .review_support import OPERATOR, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world", "photo_world"]


def _spec(world: GenWorld, key: str) -> dict[str, Any]:
    """The current Amazon kurta template's spec for `key`, as stored."""
    found = world.scalar(
        "SELECT attributes->'specs'->%s FROM category_templates WHERE marketplace = 'amazon' "
        "AND category = 'ethnic_wear_kurta_sets' AND is_current",
        [key],
    )
    assert isinstance(found, dict)
    return found


def _spellings(template: CategoryTemplate, key: str) -> list[str]:
    return list(
        dict.fromkeys(pf.template_spelling(key, v, template) for v in pf.allowed_for(key, template))
    )


async def _answer(api: httpx.AsyncClient, world: GenWorld, task_id: Any, value: Any) -> Any:
    return await api.post(
        f"/api/v1/tasks/{task_id}/answer",
        headers=world.headers(OPERATOR),
        json={"value": value},
    )


async def test_photo_question_offers_the_template_subset(
    api: httpx.AsyncClient, photo_world: PhotoWorld
) -> None:
    pw = photo_world
    plain = await generate(pw, None)
    regenerable(pw, plain.listing_id)
    await generate(pw, "photo_low")
    (task,) = [t for t in pw.tasks() if t["kind"] == "missing_fact"]
    allowed = task["expected_answer"]["allowed_values"]
    assert allowed == _spellings(KURTA, "color")
    spec = _spec(pw.world, "color")
    accepted = {v.strip().casefold() for v in [*(spec.get("allowed_values") or [])]}
    accepted |= {v.strip().casefold() for v in spec.get("value_map") or {}}
    assert all(v.strip().casefold() in accepted for v in allowed)
    outside = await _answer(api, pw.world, task["id"], "Mustard Yellow")
    assert outside.status_code == 422, outside.text
    inside = await _answer(api, pw.world, task["id"], allowed[0])
    assert inside.status_code == 200, inside.text
    # stored as the dictionary normalises it; the Rules check folds `strip().casefold()`
    assert str(pw.attributes()["color"]["value"]).strip().casefold() in accepted


async def test_rules_question_offers_the_template_subset(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    set_status(gen_world, made.listing_id, "needs_review")
    row = gen_world.listing(made.listing_id)
    content = gen_world.scalar(
        "SELECT content_json FROM listing_versions WHERE id = %s", [row["current_version_id"]]
    )
    attributes = {k: v for k, v in content["attributes"].items() if k != "color"}
    headers = gen_world.headers(OPERATOR)
    headers.pop("Idempotency-Key")
    headers["If-Match"] = f'"{row["current_version_id"]}"'
    response = await api.patch(
        f"/api/v1/listings/{made.listing_id}/content",
        headers=headers,
        json={"content": content["content"], "attributes": attributes},
    )
    assert response.status_code == 200, response.text
    task = gen_world.conn.execute(
        "SELECT t.expected_answer FROM human_tasks t JOIN listing_issues i "
        "ON i.human_task_id = t.id WHERE i.listing_id = %s AND i.field_path = 'attributes.color' "
        "AND i.source = 'rules_engine'",
        [made.listing_id],
    ).fetchone()
    assert task is not None
    expected = task[0]
    assert expected["kind"] == "enum"
    spec = _spec(gen_world, "color")
    spellings = [*(spec.get("allowed_values") or []), *sorted(spec.get("value_map") or {})]
    assert expected["allowed_values"] == list(dict.fromkeys(spellings))
