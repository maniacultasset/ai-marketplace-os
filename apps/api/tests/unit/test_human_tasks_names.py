"""SI3 round 1 F2: the photo screen's task opener has its own name, so it never collides with the
SI4 lane's `open_fact_task(product_id, target_path, question, reason, expected_answer, *,
colourway_id) -> UUID`, and the live-status tuple carries SI4's name (`LIVE`), so both lanes
merge into one definition."""

from __future__ import annotations

import inspect

from app.repositories import human_tasks
from app.repositories.human_tasks import HumanTaskRepository


def test_the_photo_task_opener_has_its_own_name() -> None:
    params = inspect.signature(HumanTaskRepository.open_photo_fact_task).parameters
    assert {"kind", "suggestions", "evidence", "image_id"} <= set(params)
    assert all(
        params[name].kind is inspect.Parameter.KEYWORD_ONLY for name in params if name != "self"
    )
    other = getattr(HumanTaskRepository, "open_fact_task", None)
    # only SI4's opener may hold that name, and it takes no `kind`
    assert other is None or "kind" not in inspect.signature(other).parameters


def test_the_live_statuses_use_the_shared_name() -> None:
    assert human_tasks.LIVE == ("open", "answered")
    assert not hasattr(human_tasks, "LIVE_STATUSES")
