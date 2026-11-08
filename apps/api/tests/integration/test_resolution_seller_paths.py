"""SI11 round 4 (R4-1, the tester's repro moved): a marketplace ERROR on a path the seller inbox
cannot ask about (`listing`, `brand.*`) on a code the fix policy routes to the seller is a staff
step with a linked task, never a `seller` step that opens nothing and stalls."""

from __future__ import annotations

import pytest
from mkos_schemas.codes import ErrorCode

from .conftest import LOCAL_HOSTS
from .preview_support import prev
from .publish_support import PubWorld
from .test_resolution_lanes import _error, _previewed, _tasks

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev"]


@pytest.mark.parametrize(
    ("field", "code", "raw"),
    [
        ("listing", ErrorCode.PRICE_ABOVE_MRP, "8541"),
        ("listing", ErrorCode.REQUIRED_FIELD_MISSING, "90220"),
        ("brand.name", ErrorCode.VALUE_NOT_ALLOWED, "8560"),
        # a fact key the planner sends to the seller but the dictionary has no row for: the
        # inbox opens no question, so the row goes to staff (the belt-and-braces check)
        ("attributes.zz_not_in_the_dictionary", ErrorCode.MISSING_ATTRIBUTE, "90220"),
    ],
)
async def test_a_seller_step_with_no_question_still_opens_a_task(
    prev: PubWorld, field: str, code: ErrorCode, raw: str
) -> None:
    made = await _previewed(prev, [_error(field, code, raw, "This listing is not accepted.")])
    assert prev.listing(made.listing_id)["status"] == "blocked"
    tasks = _tasks(prev, made.listing_id)
    assert tasks, f"{field}/{code}: open marketplace ERROR with no task, no question, no repair"
    assert all(t["kind"] != "missing_fact" for t in tasks), tasks
