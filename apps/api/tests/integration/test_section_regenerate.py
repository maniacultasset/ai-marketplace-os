"""Client feedback 14 and 16 (11 Nov 2026): a seller's section regenerate, field locks, the
answers' affected sections and the run's reason and scope, against the demo seed as `app_rw`.

- the regenerate service with `sections`: the `mode sections` payload on the current version,
  locked paths dropped, every path locked -> 422 on `sections`, no version -> 409;
- the content PATCH locks every path it edits; `unlock` alone releases a lock with no version;
- the detail names the sections whose facts were answered after the last AI run that wrote them
  (an edit or another section's regenerate never hides them);
- the job: only the sections change, a locked path never changes on a whole or section
  regenerate, the whole regenerate that kept a lock keeps the editor out of the approval, and
  every `agent_runs` row carries the reason and scope.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from mkos_schemas.codes import ErrorCode

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, set_status
from .review_support import OWNER, generated, run_job
from .test_listings_content_patch_api import CLEAN, _headers, _patch, _seeded

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
OPERATOR = "operator@acme.test"
MODEL_TITLE = "Model Written Cotton Kurta Set Title"
MODEL_DESCRIPTION = "A description the model wrote for this cotton kurta set."


async def _regenerate(
    world: GenWorld,
    listing: uuid.UUID,
    sections: tuple[Any, ...] = (),
    *,
    for_answers: bool = False,
    email: str = OPERATOR,
) -> Any:
    from app.repositories.uow import tenant_uow
    from app.services.generation import request_regeneration

    principal = world.principal(email)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        return await request_regeneration(
            uow, principal, str(uuid.uuid4()), listing, None, sections, for_answers=for_answers
        )


def _payload(world: GenWorld, job_id: uuid.UUID) -> dict[str, Any]:
    raw = world.scalar("SELECT payload_json FROM job_runs WHERE id = %s", [job_id])
    return raw if isinstance(raw, dict) else json.loads(raw)


def _content(world: GenWorld, version_id: Any) -> dict[str, Any]:
    raw = world.scalar("SELECT content_json FROM listing_versions WHERE id = %s", [version_id])
    return raw if isinstance(raw, dict) else json.loads(raw)


def _lock(world: GenWorld, listing: uuid.UUID, paths: list[str]) -> None:
    world.conn.execute("UPDATE listings SET locked_paths = %s WHERE id = %s", [paths, listing])


# ---- the service ----


async def test_a_section_regenerate_enqueues_the_sections_job(gen_world: GenWorld) -> None:
    listing, version = _seeded(gen_world)
    accepted = await _regenerate(gen_world, listing, ("description", "title"))
    payload = _payload(gen_world, accepted.job_id)
    assert payload == {
        "v": 1,
        "listing_id": str(listing),
        "mode": "sections",
        "base_version_id": str(version),
        "target_fields": ["content.title", "content.description"],
        "reason": "section_regenerate",
    }
    row = gen_world.listing(listing)
    assert (row["status"], row["last_event"], row["approved_version_id"]) == (
        "generating",
        "regenerate",
        None,
    )
    assert gen_world.scalar(
        "SELECT regeneration_job_id FROM review_decisions WHERE listing_id = %s", [listing]
    ) == (accepted.job_id)


async def test_update_these_sections_records_answers_applied(gen_world: GenWorld) -> None:
    listing, _version = _seeded(gen_world)
    accepted = await _regenerate(gen_world, listing, ("bullets",), for_answers=True)
    payload = _payload(gen_world, accepted.job_id)
    assert payload["reason"] == "answers_applied"
    assert payload["target_fields"] == ["content.bullets"]


async def test_locked_paths_are_never_targets(gen_world: GenWorld) -> None:
    listing, _version = _seeded(gen_world)
    _lock(gen_world, listing, ["content.title", "keywords.search_terms"])
    accepted = await _regenerate(gen_world, listing, ("title", "keywords"))
    assert _payload(gen_world, accepted.job_id)["target_fields"] == ["keywords.hidden_terms"]


async def test_every_path_locked_is_422_on_sections(gen_world: GenWorld) -> None:
    from app.core.errors import InputError

    listing, _version = _seeded(gen_world)
    _lock(gen_world, listing, ["content.title"])
    with pytest.raises(InputError) as caught:
        await _regenerate(gen_world, listing, ("title",))
    assert caught.value.field == "sections"
    assert gen_world.count("job_runs") == 0
    assert gen_world.listing(listing)["status"] == "validated"


async def test_a_section_regenerate_needs_a_version(gen_world: GenWorld) -> None:
    from app.core.errors import ConflictError

    listing, _version = _seeded(gen_world)
    gen_world.conn.execute("UPDATE listings SET current_version_id = NULL WHERE id = %s", [listing])
    set_status(gen_world, listing, "failed")
    with pytest.raises(ConflictError) as caught:
        await _regenerate(gen_world, listing, ("title",))
    assert caught.value.code is ErrorCode.INVALID_STATUS_TRANSITION
    assert gen_world.count("job_runs") == 0


# ---- the API: locks, unlock, the regenerate body, affected sections ----


async def test_an_edit_locks_what_it_changed_and_unlock_releases_it(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    listing, version = _seeded(gen_world)
    edited = await _patch(
        api, gen_world, listing, version, {"content": {**CLEAN, "title": "My own kurta title"}}
    )
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert "content.title" in body["locked_paths"]
    assert body["affected_sections"] == []
    assert gen_world.listing(listing)["locked_paths"] == body["locked_paths"]
    current = body["current_version_id"]
    versions = gen_world.count("listing_versions", "listing_id = %s", [listing])

    # unlock only: the same content, no new version, the lock gone
    content = body["content"]["content"]
    unlocked = await _patch(
        api, gen_world, listing, current, {"content": content, "unlock": ["content.title"]}
    )
    assert unlocked.status_code == 200, unlocked.text
    assert "content.title" not in unlocked.json()["locked_paths"]
    assert gen_world.count("listing_versions", "listing_id = %s", [listing]) == versions
    assert unlocked.json()["current_version_id"] == current
    # the unlock is audited (who released it, the paths only)
    assert gen_world.scalar(
        "SELECT reason FROM audit_log WHERE action = 'listing.unlocked' AND entity_id = %s",
        [listing],
    ) == ("unlocked: content.title")


async def test_unlock_takes_lockable_paths_only(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    listing, version = _seeded(gen_world)
    response = await api.patch(
        f"/api/v1/listings/{listing}/content",
        headers=_headers(gen_world, version),
        json={"content": CLEAN, "unlock": ["images[0]"]},
    )
    assert response.status_code == 422, response.text


async def test_the_regenerate_route_takes_sections(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    listing, _version = _seeded(gen_world)
    url = f"/api/v1/listings/{listing}/regenerate"
    bad = await api.post(
        url, headers=gen_world.headers(OPERATOR), json={"sections": ["title", "title"]}
    )
    assert bad.status_code == 422, bad.text
    lone = await api.post(url, headers=gen_world.headers(OPERATOR), json={"for_answers": True})
    assert lone.status_code == 422, lone.text
    ok = await api.post(url, headers=gen_world.headers(OPERATOR), json={"sections": ["attributes"]})
    assert ok.status_code == 202, ok.text
    payload = _payload(gen_world, uuid.UUID(ok.json()["job_id"]))
    assert payload["mode"] == "sections" and payload["target_fields"] == ["attributes"]


def _answered(
    world: GenWorld, product: uuid.UUID, key: str, *, after_version: bool, mins: int = 1
) -> None:
    world.conn.execute(
        "INSERT INTO human_tasks (tenant_id, kind, product_id, target_path, question, reason, "
        "expected_answer, status, answer, answered_by_user_id, answered_at, resume_from) "
        "VALUES (%s, 'missing_fact', %s, %s, 'What is it made of?', 'The listing needs it.', "
        "'{\"kind\": \"text\"}', 'answered', '\"cotton\"', %s, "
        "now() + make_interval(mins => %s), 'generate')",
        [
            world.tenant("acme"),
            product,
            f"facts.attributes.{key}",
            world.user(OWNER),
            mins if after_version else -24 * 60,
        ],
    )


async def test_the_detail_names_the_sections_the_answers_feed(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    listing, _version = _seeded(gen_world)
    product = gen_world.product("acme", "KURTA-001")
    _answered(gen_world, product, "country_of_origin", after_version=False)  # older: ignored
    url = f"/api/v1/listings/{listing}"
    first = await api.get(url, headers=gen_world.headers(OPERATOR))
    assert first.status_code == 200, first.text
    assert first.json()["affected_sections"] == []

    _answered(gen_world, product, "wash_care", after_version=True)
    _lock(gen_world, listing, ["content.description"])
    second = await api.get(url, headers=gen_world.headers(OPERATOR))
    assert second.json()["affected_sections"] == ["bullets", "keywords", "attributes"]
    assert second.json()["locked_paths"] == ["content.description"]


# ---- the job ----


@pytest.fixture
def model_rewrites(monkeypatch: pytest.MonkeyPatch) -> None:
    """The model rewrites the title and the description on every run."""
    from app.workers import generation

    real = getattr(generation, "run_listing_pipeline")  # noqa: B009 - the module re-exports none

    async def rewriting(ctx: Any, deps: Any) -> Any:
        result = await real(ctx, deps)
        if result.listing is None:
            return result
        content = result.listing.content.model_copy(
            update={"title": MODEL_TITLE, "description": MODEL_DESCRIPTION}
        )
        return result.model_copy(
            update={"listing": result.listing.model_copy(update={"content": content})}
        )

    monkeypatch.setattr(generation, "run_listing_pipeline", rewriting)


def _runs(world: GenWorld, job_id: Any) -> list[tuple[Any, ...]]:
    return world.conn.execute(
        # the version's generation calls (a brand lookup's embedding call is linked to no
        # version and records no reason)
        "SELECT run_reason, run_scope FROM agent_runs "
        "WHERE job_id = %s AND listing_version_id IS NOT NULL",
        [job_id],
    ).fetchall()


@pytest.mark.usefixtures("model_rewrites")
async def test_a_section_job_changes_only_its_sections_and_keeps_locks(
    gen_world: GenWorld,
) -> None:
    made = await generated(gen_world, OPERATOR)
    assert {r[0] for r in _runs(gen_world, made.job_id)} == {"generate"}
    assert {r[1] for r in _runs(gen_world, made.job_id)} == {None}
    base = _content(gen_world, made.version_id)
    _lock(gen_world, made.listing_id, ["content.title"])

    accepted = await _regenerate(gen_world, made.listing_id, ("description",))
    await run_job(accepted.job_id)
    row = gen_world.listing(made.listing_id)
    version = gen_world.conn.execute(
        "SELECT origin, changed_paths FROM listing_versions WHERE id = %s",
        [row["current_version_id"]],
    ).fetchone()
    assert version == ("regeneration", ["content.description"])
    out = _content(gen_world, row["current_version_id"])
    assert out["content"]["description"] == MODEL_DESCRIPTION
    assert out["content"]["title"] == base["content"]["title"]  # locked, and not a target
    assert out["content"]["bullets"] == base["content"]["bullets"]
    assert out["keywords"] == base["keywords"]
    runs = _runs(gen_world, accepted.job_id)
    assert runs and {(r, tuple(s)) for r, s in runs} == {
        ("section_regenerate", ("content.description",))
    }


@pytest.mark.usefixtures("model_rewrites")
async def test_a_whole_regenerate_keeps_a_lock_and_its_editor_out_of_the_approval(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world, OPERATOR)
    base = _content(gen_world, made.version_id)
    edit = {**base["content"], "title": "The Owner's Own Cotton Kurta Set Title"}
    edited = await api.patch(
        f"/api/v1/listings/{made.listing_id}/content",
        headers=_headers(gen_world, made.version_id, OWNER),
        json={"content": edit},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["locked_paths"] == ["content.title"]

    accepted = await _regenerate(gen_world, made.listing_id)
    await run_job(accepted.job_id)
    row = gen_world.listing(made.listing_id)
    out = _content(gen_world, row["current_version_id"])
    assert out["content"]["title"] == edit["title"]  # the owner's locked edit survives
    assert out["content"]["description"] == MODEL_DESCRIPTION
    changed = gen_world.scalar(
        "SELECT changed_paths FROM listing_versions WHERE id = %s", [row["current_version_id"]]
    )
    assert changed and "content.title" not in changed  # not a fresh generation
    assert {r[0] for r in _runs(gen_world, accepted.job_id)} == {"regenerate"}
    if row["status"] in ("validated", "needs_review"):
        detail = await api.get(
            f"/api/v1/listings/{made.listing_id}", headers=gen_world.headers(OWNER)
        )
        assert detail.json()["can_approve"] is False  # the owner's text is in it


@pytest.mark.usefixtures("model_rewrites")
async def test_affected_sections_outlive_an_edit_and_clear_only_what_ran(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world, OPERATOR)
    product = gen_world.listing(made.listing_id)["product_id"]
    _answered(gen_world, product, "wash_care", after_version=True, mins=0)  # now: after the run
    url = f"/api/v1/listings/{made.listing_id}"
    described = ["bullets", "description", "keywords", "attributes"]
    first = await api.get(url, headers=gen_world.headers(OPERATOR))
    assert first.json()["affected_sections"] == described

    # a seller's edit is a new version, not an AI write: the answers still wait
    base = _content(gen_world, made.version_id)
    edited = await api.patch(
        f"{url}/content",
        headers=_headers(gen_world, made.version_id, OWNER),
        json={"content": {**base["content"], "title": "The Owner's Own Cotton Kurta Set Title"}},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["affected_sections"] == described

    # one section regenerated: only that section is up to date
    accepted = await _regenerate(gen_world, made.listing_id, ("bullets",), for_answers=True)
    await run_job(accepted.job_id)
    after = await api.get(url, headers=gen_world.headers(OPERATOR))
    assert after.json()["affected_sections"] == ["description", "keywords", "attributes"]
