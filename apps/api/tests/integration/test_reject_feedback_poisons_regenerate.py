"""S19 fix round 1 (tester repro, CP12): every item's feedback gets the secret-shaped check, not
only a reject with `regenerate: true`. A plain reject's feedback joins
`GenerationContext.review_feedback` (the newest 5 rejects) on every later regeneration, whose seam
validator refuses a secret-shaped key; stored, it would fail each later regeneration of the listing
(`generation.context_refused`). So it is a 422 before any write: no batch, no decision."""

from __future__ import annotations

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .review_support import REVIEWER, generated, item, post_approve

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]

SECRET_SHAPED = 'Please fix {"api_key": "sk-not-a-real-key-000000"} in the title'


@pytest.mark.parametrize(
    "extra",
    [
        {"decision": "reject", "reason_code": "poor_quality", "regenerate": False},
        {"decision": "reject", "reason_code": "poor_quality"},
        {"decision": "approve"},
    ],
    ids=["plain_reject", "reject_default_regenerate", "approve_with_feedback"],
)
async def test_secret_shaped_feedback_422_without_regenerate(
    api: httpx.AsyncClient, gen_world: GenWorld, extra: dict[str, object]
) -> None:
    made = await generated(gen_world)
    rest = {k: v for k, v in extra.items() if k != "decision"}
    body = item(made, str(extra["decision"]), feedback=SECRET_SHAPED, **rest)
    response = await post_approve(api, gen_world, REVIEWER, [body])
    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"
    assert "sk-not-a-real-key" not in response.text
    assert gen_world.count("batches") == 0
    assert gen_world.count("review_decisions", "listing_id = %s", [made.listing_id]) == 0
    assert gen_world.listing(made.listing_id)["status"] == "validated"
