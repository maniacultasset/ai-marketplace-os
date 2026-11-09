"""SI14 T14 (AC6): the seller's category write settles the suggestion (`confirmed` or
`overridden`) and answers then applies the live `category_review` task."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from psycopg.types.json import Jsonb

from . import factories as f
from .assist_support import review_tasks, seed_templates, suggestion
from .conftest import LOCAL_HOSTS
from .images_support import images_db
from .jobs_support import JobWorld, as_member

__all__ = ["images_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
PREFIX = "/api/v1/products"
OPERATOR = "operator@acme.test"


@pytest.fixture(autouse=True)
def _templates(images_db: JobWorld) -> None:
    seed_templates(images_db.conn, "t_shirts", "shirts", "kurtas")


def _suggested(jw: JobWorld, *, with_task: bool = True) -> tuple[uuid.UUID, uuid.UUID | None]:
    tenant = jw.tenant("acme")
    p = f.product(jw.conn, tenant, category=None, provenance={"title": f.fact()})
    jw.conn.execute(
        "UPDATE products SET category_suggestion = %s WHERE id = %s",
        [
            Jsonb(
                {
                    "status": "suggested",
                    "candidates": [
                        {
                            "category": "t_shirts",
                            "confidence": 0.6,
                            "rationale": "A tee.",
                            "evidence_refs": [],
                        },
                        {
                            "category": "shirts",
                            "confidence": 0.3,
                            "rationale": "Collared?",
                            "evidence_refs": [],
                        },
                    ],
                    "input_digest": "a" * 64,
                    "calibration_version": "v1",
                    "job_id": None,
                    "at": datetime.now(UTC).isoformat(),
                }
            ),
            p,
        ],
    )
    task = None
    if with_task:
        task = f.human_task(
            jw.conn,
            tenant,
            product_id=p,
            kind="category_review",
            target_path=None,
            question="Which category fits this product?",
            reason="The suggested categories are not certain enough.",
            expected_answer={"kind": "enum", "allowed": ["t_shirts", "shirts", "kurtas"]},
            suggestions=Jsonb(["t_shirts", "shirts"]),
        )
    return p, task


async def _patch(
    api: httpx.AsyncClient, jw: JobWorld, product_id: uuid.UUID, body: dict[str, Any]
) -> httpx.Response:
    hdrs = as_member(jw, OPERATOR)
    etag = (await api.get(f"{PREFIX}/{product_id}", headers=hdrs)).headers["ETag"]
    return await api.patch(f"{PREFIX}/{product_id}", headers={**hdrs, "If-Match": etag}, json=body)


def _task(jw: JobWorld, task_id: uuid.UUID | None) -> dict[str, Any]:
    cur = jw.conn.execute("SELECT * FROM human_tasks WHERE id = %s", [task_id])
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


async def test_patch_to_a_candidate_confirms_and_applies_the_task(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    p, task = _suggested(images_db)
    response = await _patch(api, images_db, p, {"category": "t_shirts"})
    assert response.status_code == 200, response.text
    assert response.json()["category"] == "t_shirts"
    assert response.json()["suggestions"]["category"]["status"] == "confirmed"
    stored = suggestion(images_db.conn, p)
    assert stored is not None and stored["status"] == "confirmed"
    row = _task(images_db, task)
    assert row["status"] == "applied" and row["answer"] == {"category": "t_shirts"}
    assert row["answered_by_user_id"] == images_db.world.user(OPERATOR)


async def test_patch_to_another_category_overrides(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    p, task = _suggested(images_db)
    response = await _patch(api, images_db, p, {"category": "kurtas"})
    assert response.status_code == 200, response.text
    assert suggestion(images_db.conn, p)["status"] == "overridden"  # type: ignore[index]
    assert _task(images_db, task)["status"] == "applied"


async def test_recategorize_settles_the_same_way(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    tenant = images_db.tenant("acme")
    p = f.product(images_db.conn, tenant, category="kurtas")
    images_db.conn.execute(
        "UPDATE products SET category_suggestion = %s WHERE id = %s",
        [
            Jsonb(
                {
                    "status": "suggested",
                    "candidates": [
                        {
                            "category": "t_shirts",
                            "confidence": 0.9,
                            "rationale": "",
                            "evidence_refs": [],
                        }
                    ],
                    "at": datetime.now(UTC).isoformat(),
                }
            ),
            p,
        ],
    )
    response = await api.post(
        f"{PREFIX}/{p}/recategorize",
        headers=as_member(images_db, OPERATOR),
        json={"category": "t_shirts"},
    )
    assert response.status_code == 200, response.text
    assert suggestion(images_db.conn, p)["status"] == "confirmed"  # type: ignore[index]


async def test_no_suggestion_and_no_task_changes_nothing(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    p = f.product(
        images_db.conn, images_db.tenant("acme"), category=None, provenance={"title": f.fact()}
    )
    response = await _patch(api, images_db, p, {"category": "shirts"})
    assert response.status_code == 200, response.text
    assert suggestion(images_db.conn, p) is None and review_tasks(images_db.conn, p) == 0


async def test_the_task_answer_api_refuses_a_category_review(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    _p, task = _suggested(images_db)
    response = await api.post(
        f"/api/v1/tasks/{task}/answer",
        headers={**as_member(images_db, OPERATOR), "Idempotency-Key": str(uuid.uuid4())},
        json={"answer": {"value": "t_shirts"}},
    )
    assert response.status_code == 422, response.text
    assert _task(images_db, task)["status"] == "open"


async def test_a_stale_replay_answers_the_task_once(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    p, task = _suggested(images_db)
    hdrs = as_member(images_db, OPERATOR)
    etag = (await api.get(f"{PREFIX}/{p}", headers=hdrs)).headers["ETag"]
    first = await api.patch(
        f"{PREFIX}/{p}", headers={**hdrs, "If-Match": etag}, json={"category": "t_shirts"}
    )
    assert first.status_code == 200
    replay = await api.patch(
        f"{PREFIX}/{p}", headers={**hdrs, "If-Match": etag}, json={"category": "shirts"}
    )
    assert replay.status_code == 409 and replay.json()["error"]["type"] == "STALE_VERSION"
    row = _task(images_db, task)
    assert row["status"] == "applied" and row["answer"] == {"category": "t_shirts"}


async def test_another_tenants_product_is_404(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    theirs = f.product(images_db.conn, images_db.tenant("bharat"))
    mine, _task_id = _suggested(images_db)
    hdrs = as_member(images_db, OPERATOR)
    etag = (await api.get(f"{PREFIX}/{mine}", headers=hdrs)).headers["ETag"]
    response = await api.patch(
        f"{PREFIX}/{theirs}", headers={**hdrs, "If-Match": etag}, json={"category": "shirts"}
    )
    assert response.status_code == 404


async def test_the_task_inbox_lists_a_category_review(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    _p, task = _suggested(images_db)
    response = await api.get("/api/v1/tasks", headers=as_member(images_db, OPERATOR))
    assert response.status_code == 200, response.text
    kinds = {t["id"]: t["kind"] for t in response.json()["data"]}
    assert kinds[str(task)] == "category_review"
