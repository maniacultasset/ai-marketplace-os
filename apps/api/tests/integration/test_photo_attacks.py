"""SI3 tester round 1: attacks on the photo facts through the real `listing_generate` job, the
stub pipeline and the services, as app_rw. Covers what the implementer's suites leave to the
repository level: a second photo run replacing its own unconfirmed value, a confirmed value on
new photos, duplicate job delivery, and the answer path on a deleted product or a re-run."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.services import photo_facts as pf
from mkos_schemas.jobs import PhotoObservation

from .conftest import LOCAL_HOSTS
from .photo_support import (
    OPERATOR,
    PhotoWorld,
    approve,
    generate,
    photo_world,
    regenerable,
    seller_patch,
    spy_pipeline,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["photo_world"]


def _swap_neckline(value_index: int, confidence: float = 0.95) -> Any:
    """An `around` hook: the real stub result with the neckline observation set to the target's
    `allowed_values[value_index]` (added when the stub emitted none for it)."""

    async def around(gctx: Any, deps: Any, real: Any) -> Any:
        result = await real(gctx, deps)
        target = next(t for t in gctx.photo_targets if t.key == "neckline")
        photo = next(i.image_id for i in gctx.images if i.slot == "main")
        kept = [o for o in result.photo_observations if o.key != "neckline"]
        swapped = PhotoObservation(
            key="neckline",
            value=target.allowed_values[value_index],
            confidence=confidence,
            image_id=photo,
        )
        return result.model_copy(update={"photo_observations": [*kept, swapped]})

    return around


async def _answer(pw: PhotoWorld, task_id: uuid.UUID, value: Any) -> dict[str, Any]:
    from app.api.v1.schemas.tasks import TaskAnswerIn
    from app.services import tasks

    principal = pw.world.principal(OPERATOR)
    return await tasks.answer_task(
        principal,
        str(uuid.uuid4()),
        f"/api/v1/tasks/{task_id}/answer",
        task_id,
        TaskAnswerIn(value=value),
    )


# ---- AC3/AC4: new photos refresh an unconfirmed photo value; the listing stays approvable ----


async def test_new_photos_replace_an_unconfirmed_photo_value(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    spy_pipeline(pw, monkeypatch)
    first = await generate(pw, "photo_ok")
    before = pw.attributes()["neckline"]
    assert before["source"] == "ai" and before.get("confirmed_at") is None
    regenerable(pw, first.listing_id)
    pw.add_image("lifestyle")
    spy_pipeline(pw, monkeypatch, _swap_neckline(1))
    again = await generate(pw, "photo_ok")
    assert {t.key: t.mode for t in pw.seen[-1].photo_targets}["neckline"] == "fill"
    after = pw.attributes()["neckline"]
    second_value = next(t for t in pw.seen[-1].photo_targets if t.key == "neckline").allowed_values[
        1
    ]
    # SI9 CP-D25: written in the template's spelling of the vocabulary value
    assert pf.canonical("neckline", after["value"]) == second_value
    assert after["value"] != before["value"]
    (record,) = pw.photo_records(again.job_id)
    assert after["agent_run_id"] == str(record["id"]) and after["source"] == "ai"
    # the re-check digest took the refreshed value: approve at once is not STALE_VERSION
    assert (await approve(pw, again.listing_id)).http_status == 200


# ---- B2/B3: a confirmed photo value is only checked on new photos, never overwritten ----


async def test_a_confirmed_photo_value_is_only_checked_and_conflicts(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    spy_pipeline(pw, monkeypatch)
    first = await generate(pw, "photo_ok")
    await seller_patch(pw, {"confirm_attributes": ["neckline"]})
    confirmed = pw.attributes()["neckline"]
    assert confirmed["confirmed_at"] is not None
    regenerable(pw, first.listing_id)
    pw.add_image("lifestyle")
    spy_pipeline(pw, monkeypatch, _swap_neckline(1))
    await generate(pw, "photo_ok")
    assert {t.key: t.mode for t in pw.seen[-1].photo_targets}["neckline"] == "check"
    assert pw.attributes()["neckline"] == confirmed
    live = [t for t in pw.tasks() if t["status"] == "open"]
    assert [(t["kind"], t["target_path"]) for t in live] == [
        ("conflicting_facts", "facts.attributes.neckline")
    ]


# ---- duplicate job delivery: a second run of a finished job writes nothing twice ----


async def test_duplicate_delivery_of_a_photo_job_writes_once(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.workers import wrapper

    pw = photo_world
    spy_pipeline(pw, monkeypatch)
    accepted = await generate(pw, "photo_mix")
    attrs = pw.attributes()
    tasks = pw.tasks()
    audits = len(pw.audit("product.fact_updated")), len(pw.audit("human_task.opened"))
    calls = len(pw.seen)
    await wrapper.run_job(str(accepted.job_id))
    assert len(pw.seen) == calls  # no second pipeline (and vision) call
    assert pw.attributes() == attrs and pw.tasks() == tasks
    assert (len(pw.audit("product.fact_updated")), len(pw.audit("human_task.opened"))) == audits
    assert len(pw.photo_records(accepted.job_id)) == 1


# ---- AC11: the answer path ----


async def test_answer_on_a_deleted_product_is_404_and_writes_nothing(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.errors import NotFoundError

    pw = photo_world
    spy_pipeline(pw, monkeypatch)
    await generate(pw, "photo_low")
    (task,) = [t for t in pw.tasks() if t["target_path"] == "facts.attributes.color"]
    pw.conn.execute("UPDATE products SET deleted_at = now() WHERE id = %s", [pw.product_id])
    with pytest.raises(NotFoundError):
        await _answer(pw, task["id"], "Navy")
    assert "color" not in pw.attributes()
    assert pw.audit("human_task.answered") == []


async def test_a_synonym_answer_is_stored_canonical_and_survives_new_photos(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    spy_pipeline(pw, monkeypatch)
    first = await generate(pw, "photo_low")
    (task,) = [t for t in pw.tasks() if t["target_path"] == "facts.attributes.color"]
    out = await _answer(pw, task["id"], "Navy")
    assert out["status"] == "applied"
    color = pw.attributes()["color"]
    assert color["source"] == "seller" and color["value"] == "navy blue"
    assert color["captured_via"] == "human_task"
    assert color["evidence_ref"] == f"human_task:{task['id']}"
    regenerable(pw, first.listing_id)
    pw.add_image("lifestyle")
    await generate(pw, "photo_ok")
    assert {t.key: t.mode for t in pw.seen[-1].photo_targets}["color"] == "check"
    assert pw.attributes()["color"] == color
