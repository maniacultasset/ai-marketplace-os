"""SI3 T12-T13 (AC3-AC10, AC12): a generation's photo targets, decision and writes, through the
real `listing_generate` job and the stub pipeline, as app_rw."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from mkos_schemas.codes import ErrorCode

from .conftest import LOCAL_HOSTS
from .photo_support import (
    PHOTO_SLOTS,
    PhotoWorld,
    approve,
    generate,
    photo_fact,
    photo_world,
    regenerable,
    seller_fact,
    seller_patch,
    simulate_publish_writes,
    spy_pipeline,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["photo_world"]
FILL_KEYS = {
    "neckline",
    "sleeve_length",
    "fit",
    "garment_length",
    "embroidery",
    "occasion",
    "color",
    "pattern",
}


def _ai(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attrs.items() if v.get("source") == "ai"}


def _targets(pw: PhotoWorld) -> dict[str, Any]:
    return {t.key: t for t in pw.seen[-1].photo_targets}


# ---- AC4, AC12: confident fills become unconfirmed ai facts, recorded and audited ----


async def test_photo_ok_writes_the_fill_keys(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    pw.drop_seller_colour(monkeypatch)
    spy_pipeline(pw, monkeypatch)
    accepted = await generate(pw, "photo_ok")
    targets = _targets(pw)
    assert {k for k, t in targets.items() if t.mode == "fill"} == FILL_KEYS
    assert targets["number_of_pieces"].mode == "check"
    run_id = uuid.uuid5(accepted.job_id, "photo:1")
    written = _ai(pw.attributes())
    assert set(written) == FILL_KEYS
    for key, fact in written.items():
        assert fact["value"] == targets[key].allowed_values[0]
        assert fact["captured_via"] == "agent" and fact["agent_run_id"] == str(run_id)
        assert fact["confidence"] == 0.95
        assert fact["evidence_ref"] == f"image:{pw.images['main']}"
        assert "confirmed_at" not in fact
    (record,) = pw.photo_records(accepted.job_id)
    assert record["id"] == run_id and record["product_id"] == pw.product_id
    assert record["agent_name"] == "image" and record["trigger"] == "vision"
    assert record["cost_usd"] == Decimal("0.004000")
    spend = pw.world.scalar(
        "SELECT sum(cost_usd) FROM agent_runs WHERE job_id = %s", [accepted.job_id]
    )
    assert spend == Decimal("0.004000")  # AC12: the stub's text calls are free
    (audit,) = pw.audit("product.fact_updated")
    assert (audit["actor_type"], audit["job_id"]) == ("system", accepted.job_id)
    assert set(audit["after"]["attributes"]) == FILL_KEYS
    assert audit["before"]["attributes"] == dict.fromkeys(FILL_KEYS)
    assert pw.world.listing(accepted.listing_id)["current_version_id"] is not None


# ---- AC5: the seller wins, and a change made during the run voids the approval ----


async def test_seller_edit_during_the_run_wins(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world

    async def around(gctx: Any, deps: Any, real: Any) -> Any:
        await seller_patch(pw, {"attributes": {"neckline": "Round Neck"}})
        return await real(gctx, deps)

    spy_pipeline(pw, monkeypatch, around)
    accepted = await generate(pw, "photo_ok")
    attrs = pw.attributes()
    assert attrs["neckline"]["source"] == "seller" and attrs["neckline"]["value"] == "Round Neck"
    assert attrs["sleeve_length"]["source"] == "ai"  # the other keys still land
    outcome = await approve(pw, accepted.listing_id)
    assert outcome.http_status == 409
    assert outcome.error is not None and outcome.error.error_code is ErrorCode.STALE_VERSION


async def test_a_seller_value_at_build_time_is_only_checked(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    pw.put_fact("neckline", seller_fact("V-Neck"))
    spy_pipeline(pw, monkeypatch)
    await generate(pw, "photo_ok")
    assert _targets(pw)["neckline"].mode == "check"
    assert pw.attributes()["neckline"] == seller_fact("V-Neck")


async def test_nothing_written_keeps_the_pre_pipeline_digest(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    accepted = await generate(pw, None)
    digests = pw.conn.execute(
        "SELECT DISTINCT facts_digest FROM validation_runs WHERE listing_id = %s",
        [accepted.listing_id],
    ).fetchall()
    assert len(digests) == 1
    assert (await approve(pw, accepted.listing_id)).http_status == 200


async def test_reject_during_the_run_wins(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    pw.put_fact("neckline", photo_fact("round neck", pw.images["main"]))

    async def around(gctx: Any, deps: Any, real: Any) -> Any:
        assert {t.key: t.mode for t in gctx.photo_targets}["neckline"] == "fill"
        await seller_patch(pw, {"attributes": {"neckline": None}})
        return await real(gctx, deps)

    spy_pipeline(pw, monkeypatch, around)
    accepted = await generate(pw, "photo_ok")
    attrs = pw.attributes()
    assert "neckline" not in attrs and attrs["fit"]["source"] == "ai"
    outcome = await approve(pw, accepted.listing_id)
    assert outcome.http_status == 409


# ---- AC9: a photo run's listing is approvable at once ----


async def test_photo_run_listing_is_approvable(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    accepted = await generate(pw, "photo_ok")
    assert _ai(pw.attributes())
    assert (await approve(pw, accepted.listing_id)).http_status == 200


# ---- AC6: hostile observations write nothing ----


async def test_photo_hostile_writes_nothing(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    accepted = await generate(pw, "photo_hostile")
    assert _ai(pw.attributes()) == {}
    assert pw.tasks() == []
    assert pw.world.listing(accepted.listing_id)["current_version_id"] is not None
    assert pw.audit("product.fact_updated") == []


# ---- AC7: questions, never a block ----


async def test_photo_low_asks_one_colour_question(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    pw.drop_seller_colour(monkeypatch)
    plain = await generate(pw, None)  # no photo record: the targets stay for the next run
    status = pw.world.listing(plain.listing_id)["status"]
    regenerable(pw, plain.listing_id)
    accepted = await generate(pw, "photo_low")
    assert pw.world.listing(accepted.listing_id)["status"] == status
    assert _ai(pw.attributes()) == {}
    (task,) = pw.tasks()
    assert (task["kind"], task["target_path"], task["status"]) == (
        "missing_fact",
        "facts.attributes.color",
        "open",
    )
    assert task["image_id"] == pw.images["main"] and task["resume_from"] == "generate"
    assert all(s["source"] == "ai" for s in task["suggestions"])
    (audit,) = pw.audit("human_task.opened")
    assert audit["reason"] == "facts.attributes.color" and audit["job_id"] == accepted.job_id
    assert audit["before"] is None and audit["after"] is None
    # a changed photo set runs vision again: the same question is not opened twice
    pw.conn.execute(
        "UPDATE images SET clean_content_hash = %s WHERE id = %s",
        ["b" * 64, pw.images["secondary"]],
    )
    regenerable(pw, accepted.listing_id)
    await generate(pw, "photo_low")
    assert len(pw.tasks()) == 1


async def test_photo_conflict_keeps_the_seller_value(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    pw.drop_seller_colour(monkeypatch)
    pw.put_fact("neckline", seller_fact("V-Neck"))
    await generate(pw, "photo_conflict")
    (task,) = pw.tasks()
    assert task["kind"] == "conflicting_facts"
    assert task["suggestions"][0] == {"value": "V-Neck", "label": "V-Neck", "source": "seller"}
    assert task["suggestions"][1]["source"] == "ai"
    assert pw.attributes()["neckline"] == seller_fact("V-Neck")


async def test_photo_pieces_never_suggests_an_ai_value(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    await generate(pw, "photo_pieces")
    (task,) = pw.tasks()
    assert task["target_path"] == "facts.attributes.number_of_pieces"
    assert task["suggestions"] == []
    assert task["evidence"][0]["kind"] == "source_value"
    assert "number_of_pieces" not in pw.attributes()


# ---- AC3, AC10: the skip rule compares photo sets ----


async def test_the_sent_set_is_recorded_and_the_second_marketplace_skips(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    spy_pipeline(pw, monkeypatch)
    amazon = await generate(pw, "photo_ok")
    assert pw.job(amazon.job_id)["result_json"]["photo_set"] == pw.sent_set()
    flipkart = await generate(pw, "photo_ok", marketplace="flipkart")
    assert pw.seen[-1].photo_targets == []
    assert pw.photo_records(flipkart.job_id) == []
    assert "photo_set" not in (pw.job(flipkart.job_id)["result_json"] or {})


@pytest.mark.parametrize("change", ["upload", "delete", "rederive"])
async def test_a_changed_photo_set_brings_the_targets_back(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    pw = photo_world
    spy_pipeline(pw, monkeypatch)
    first = await generate(pw, "photo_ok")
    regenerable(pw, first.listing_id)
    if change == "upload":
        pw.add_image("lifestyle")
    elif change == "delete":
        pw.conn.execute(
            "UPDATE images SET deleted_at = now() WHERE id = %s", [pw.images["lifestyle"]]
        )
    else:
        pw.conn.execute(
            "UPDATE images SET clean_content_hash = %s WHERE id = %s",
            ["c" * 64, pw.images["main"]],
        )
    again = await generate(pw, "photo_ok")
    assert pw.seen[-1].photo_targets != []
    assert len(pw.photo_records(again.job_id)) == 1


async def test_publish_then_regenerate_makes_no_vision_call(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    spy_pipeline(pw, monkeypatch)
    first = await generate(pw, "photo_ok")
    assert (await approve(pw, first.listing_id)).http_status == 200
    simulate_publish_writes(pw)
    again = await generate(pw, "photo_ok", marketplace="flipkart")
    assert pw.seen[-1].photo_targets == [] and pw.photo_records(again.job_id) == []


async def test_reject_then_publish_then_regenerate_is_not_proposed_again(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    spy_pipeline(pw, monkeypatch)
    await generate(pw, "photo_ok")
    await seller_patch(pw, {"attributes": {"neckline": None}})
    simulate_publish_writes(pw)
    await generate(pw, "photo_ok", marketplace="flipkart")
    assert pw.seen[-1].photo_targets == []
    assert "neckline" not in pw.attributes()


async def test_an_upload_during_the_run_is_not_in_the_recorded_set(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    added: list[uuid.UUID] = []

    async def around(gctx: Any, deps: Any, real: Any) -> Any:
        if not added:
            added.append(pw.add_image("secondary"))
        return await real(gctx, deps)

    spy_pipeline(pw, monkeypatch, around)
    first = await generate(pw, "photo_ok")
    recorded = pw.job(first.job_id)["result_json"]["photo_set"]
    assert str(added[0]) not in {pair[0] for pair in recorded}
    await generate(pw, "photo_ok", marketplace="flipkart")
    assert pw.seen[-1].photo_targets != []


# ---- AC12: a failed photo call, the budget cap ----


async def test_photo_timeout_saves_the_failed_record_only(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    accepted = await generate(pw, "photo_timeout")
    (record,) = pw.photo_records(accepted.job_id)
    assert record["status"] == "failed" and record["trigger"] == "vision"
    assert _ai(pw.attributes()) == {} and pw.tasks() == []
    assert "photo_set" not in (pw.job(accepted.job_id)["result_json"] or {})
    assert pw.world.listing(accepted.listing_id)["current_version_id"] is not None


async def test_budget_cap_refuses_before_any_call(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from . import factories as f

    pw = photo_world
    f.agent_run(pw.conn, pw.tenant, cost_usd=Decimal("2.500000"), started_at=datetime.now(UTC))
    spy_pipeline(pw, monkeypatch)
    accepted = await generate(pw, "photo_ok")
    job = pw.job(accepted.job_id)
    assert (job["status"], job["error_code"]) == ("failed", "BUDGET_EXCEEDED")
    assert pw.seen == []


# ---- partial failure and the no-version path ----


async def test_one_refused_write_keeps_the_others_and_the_version(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.errors import InputError
    from app.repositories.facts import FactWriter

    pw = photo_world
    pw.drop_seller_colour(monkeypatch)
    real = FactWriter.set_photo_attribute

    async def refuse_neckline(self: Any, product_id: Any, key: str, fact: Any, **kw: Any) -> bool:
        if key == "neckline":
            raise InputError(ErrorCode.FACT_SOURCE_FORBIDDEN, f"attributes.{key}")
        return await real(self, product_id, key, fact, **kw)

    monkeypatch.setattr(FactWriter, "set_photo_attribute", refuse_neckline)
    accepted = await generate(pw, "photo_ok")
    assert set(_ai(pw.attributes())) == FILL_KEYS - {"neckline"}
    assert pw.world.listing(accepted.listing_id)["current_version_id"] is not None


async def test_failed_scenario_with_targets_writes_nothing(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    spy_pipeline(pw, monkeypatch)
    accepted = await generate(pw, "failed")
    assert pw.seen[-1].photo_targets != []
    assert _ai(pw.attributes()) == {}
    assert pw.job(accepted.job_id)["result_json"] is None


async def test_no_version_result_with_a_photo_record_still_writes(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    pw.drop_seller_colour(monkeypatch)

    async def around(gctx: Any, deps: Any, real: Any) -> Any:
        ok = await real(gctx.model_copy(update={"user_instructions": "stub:photo_ok"}), deps)
        failed = await real(gctx.model_copy(update={"user_instructions": "stub:failed"}), deps)
        photo = [r for r in ok.agent_runs if (r.prompt_version or "").startswith("photo_facts/")]
        return failed.model_copy(
            update={
                "agent_runs": [*photo, *failed.agent_runs],
                "photo_observations": ok.photo_observations,
            }
        )

    spy_pipeline(pw, monkeypatch, around)
    accepted = await generate(pw, None)
    job = pw.job(accepted.job_id)
    assert (job["status"], job["error_code"]) == ("failed", "GENERATION_FAILED")
    assert job["result_json"]["photo_set"] == pw.sent_set()
    assert set(_ai(pw.attributes())) == FILL_KEYS


async def test_photo_slots_are_the_only_ones_sent_in_the_set(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    accepted = await generate(pw, "photo_ok")
    recorded = {pair[0] for pair in pw.job(accepted.job_id)["result_json"]["photo_set"]}
    assert recorded == {str(pw.images[s]) for s in PHOTO_SLOTS}


# ---- T13 (AC8): an unconfirmed photo value never backs a claim; a confirmed one does ----


async def test_photo_inject_needs_a_confirmed_construction(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.resolved_facts import for_rules
    from app.repositories.uow import tenant_uow
    from app.services.generation_result import recheck
    from app.services.rules_loader import load_rules
    from mkos_schemas.listing import CanonicalListing

    pw = photo_world
    pw.put_fact("construction", photo_fact("handmade", pw.images["main"]))
    accepted = await generate(pw, "photo_inject")
    runs = pw.conn.execute(
        "SELECT error_report_json FROM validation_runs WHERE listing_id = %s "
        "AND listing_version_id IS NOT NULL",
        [accepted.listing_id],
    ).fetchall()
    codes = {e["error_code"] for (report,) in runs for e in report["errors"]}
    assert "UNSUPPORTED_CLAIM" in codes
    assert pw.attributes()["construction"]["source"] == "ai"  # the observation was no target
    await seller_patch(pw, {"confirm_attributes": ("construction",)})
    row = pw.world.listing(accepted.listing_id)
    content = pw.world.scalar(
        "SELECT content_json FROM listing_versions WHERE id = %s", [row["current_version_id"]]
    )
    async with tenant_uow(pw.tenant) as uow:
        rules = await load_rules(
            uow, tenant_id=pw.tenant, marketplace="amazon", category=row["category"]
        )
        facts = await uow.product_facts.resolve(pw.product_id)
    assert facts is not None
    report = recheck(CanonicalListing.model_validate(content), rules, for_rules(facts))
    assert "UNSUPPORTED_CLAIM" not in {e.error_code.value for e in report.errors}
