"""SI14 (AC6): a CSV import that writes the category of a stored product settles its suggestion
and applies its live `category_review` task, like PATCH and recategorize do."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from psycopg.types.json import Jsonb

from . import factories as f
from .assist_support import review_tasks, suggestion
from .conftest import LOCAL_HOSTS
from .imports_support import MAPPED, csv_bytes, import_fixture, imports_db, row, run_job
from .jobs_support import JobWorld

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["imports_db"]


def _suggested(jw: JobWorld, sku: str, category: str | None) -> uuid.UUID:
    tenant = jw.tenant("acme")
    p = f.product(jw.conn, tenant, sku=sku, category=category)
    jw.conn.execute(
        "UPDATE products SET category_suggestion = %s WHERE id = %s",
        [
            Jsonb(
                {
                    "status": "suggested",
                    "candidates": [
                        {
                            "category": MAPPED[0],
                            "confidence": 0.6,
                            "rationale": "A kurta set.",
                            "evidence_refs": [],
                        }
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
    if category is None:
        f.human_task(
            jw.conn,
            tenant,
            product_id=p,
            kind="category_review",
            target_path=None,
            question="Which category fits this product?",
            reason="The suggested categories are not certain enough.",
            expected_answer={"kind": "enum", "allowed": list(MAPPED)},
            suggestions=Jsonb([MAPPED[0]]),
        )
    return p


@pytest.mark.parametrize(
    ("category", "status"), [(MAPPED[0], "confirmed"), (MAPPED[1], "overridden")]
)
async def test_import_category_settles_the_suggestion_and_applies_the_review(
    imports_db: JobWorld, api: httpx.AsyncClient, category: str, status: str
) -> None:
    p = _suggested(imports_db, "SKU-00001", None)
    _, job = await import_fixture(imports_db, api, csv_bytes([row(1, category=category)]))
    await run_job(job)
    stored = suggestion(imports_db.conn, p)
    assert stored is not None and stored["status"] == status
    assert review_tasks(imports_db.conn, p, "applied") == 1
    assert review_tasks(imports_db.conn, p, "open") == 0


async def test_import_with_the_same_category_leaves_the_suggestion(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    p = _suggested(imports_db, "SKU-00001", MAPPED[1])
    _, job = await import_fixture(imports_db, api, csv_bytes([row(1, category=MAPPED[1])]))
    await run_job(job)
    assert suggestion(imports_db.conn, p)["status"] == "suggested"  # type: ignore[index]
