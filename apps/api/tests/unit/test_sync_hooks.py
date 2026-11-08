"""SI11 T7 (CP-D16): the `listing_status_sync` terminal hook routes by body; a dry run never
counts a poll, a status read still does, and both then enqueue a pending check."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from app.services import job_hooks, remote_preview
from app.services import status_sync_outcome as sso


def _job(op: str | None) -> Any:
    payload: dict[str, Any] = {"v": 1, "listing_id": str(uuid.uuid4())}
    if op is not None:
        payload["op"] = op
    return SimpleNamespace(
        kind="listing_status_sync",
        status="dead_letter",
        payload_json=payload,
        listing_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
    )


@pytest.mark.parametrize(
    ("op", "polls", "previews"), [(None, 1, 0), ("sync", 1, 0), ("preview", 0, 1)]
)
async def test_hook_routes_by_op(
    monkeypatch: pytest.MonkeyPatch, op: str | None, polls: int, previews: int
) -> None:
    calls: dict[str, int] = {"poll": 0, "preview": 0, "pending": 0}

    async def poll(session: Any, job: Any, outcome: Any) -> None:
        calls["poll"] += 1

    async def preview(session: Any, job: Any, outcome: Any) -> None:
        calls["preview"] += 1

    async def pending(session: Any, job: Any) -> None:
        calls["pending"] += 1

    monkeypatch.setattr(sso, "count_failed_poll", poll)
    monkeypatch.setattr(remote_preview, "on_terminal", preview)
    monkeypatch.setattr(remote_preview, "enqueue_pending", pending)
    await job_hooks.on_sync_terminal(object(), _job(op), None)
    assert calls == {"poll": polls, "preview": previews, "pending": 1}
