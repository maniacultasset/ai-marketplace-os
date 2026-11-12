"""Client item 20 bullet 3 (12 Nov): with the brand's auto-fix off, Kuinn keeps a safe fix as a
suggestion and applies it only on the seller's "Apply fixes". End to end on the in-process
adapter, as `test_auto_resolution.py` (a stored PTD enum is simulated so the repairer folds the
colour)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .preview_support import prev, preview_jobs
from .products_support import bad_bearer, bearer_for
from .publish_support import PubWorld, run_job
from .review_support import OWNER, REVIEWER, generated
from .test_auto_resolution import _colour_error, _issue_status

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev"]


@pytest.fixture
def marketplace_enum(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.resolution import run

    async def enums(uow: Any, source: Any, module: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return {"color": ("RED", "BLUE", "NAVY", "MAROON")}, {}

    monkeypatch.setattr(run, "_enums", enums)


def _off(pw: PubWorld) -> None:
    pw.conn.execute(
        "UPDATE tenants SET auto_fix_enabled = false WHERE id = %s", [pw.world.tenant("acme")]
    )


def _repairs(pw: PubWorld, listing_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return pw.rows(
        "SELECT rule, attribute, held, retired_at FROM listing_repairs WHERE listing_id = %s",
        [listing_id],
    )


def _audit(pw: PubWorld, action: str) -> list[tuple[Any, ...]]:
    return pw.rows(
        "SELECT actor_type, actor_user_id FROM audit_log WHERE action = %s ORDER BY occurred_at",
        [action],
    )


async def test_off_keeps_the_fix_as_a_suggestion(
    api: httpx.AsyncClient, prev: PubWorld, marketplace_enum: None
) -> None:
    _off(prev)
    prev.fake.preview_errors = [_colour_error()]
    made = await generated(prev.world)
    (first,) = preview_jobs(prev, made.listing_id)
    await run_job(first["id"])
    assert _repairs(prev, made.listing_id) == [("enum_fold", "color", True, None)]
    assert len(preview_jobs(prev, made.listing_id)) == 1  # no automatic re-check
    ((_path, status, _resolution, _action),) = _issue_status(prev, made.listing_id)
    assert status == "open"
    assert _audit(prev, "listing_repair.suggested") == [("system", None)]
    assert _audit(prev, "listing_repair.created") == []

    auth = bearer_for(prev.world, OWNER)
    history = await api.get(f"/api/v1/listings/{made.listing_id}/issue-history", headers=auth)
    assert history.status_code == 200, history.text
    body = history.json()
    (fix,) = body["fixes"]
    assert (fix["suggested"], fix["outcome"]) == (True, "waiting")
    (issue,) = body["issues"]
    assert (issue["handler"], issue["lane"]) == ("you", "seller")
    assert body["counts"]["fixed"] == 0 and body["counts"]["needs_you"] == 1

    # a replayed hook does not suggest twice
    from app.repositories.uow import tenant_uow
    from app.services.resolution.run import resolve

    async with tenant_uow(prev.world.tenant("acme")) as uow:
        listing = await uow.listings.get(made.listing_id)
        job = await uow.job_runs.get_by_id(first["id"])
        assert job is not None
        await resolve(uow, listing, entry="preview", job=job)
    assert len(_repairs(prev, made.listing_id)) == 1
    assert len(_audit(prev, "listing_repair.suggested")) == 1


@pytest.mark.endpoint("listings_fixes_apply", case="success")
@pytest.mark.endpoint("listings_fixes_apply", case="wrong_role")
@pytest.mark.endpoint("listings_fixes_apply", case="conflict")
async def test_the_sellers_apply_releases_it_and_rechecks(
    api: httpx.AsyncClient, prev: PubWorld, marketplace_enum: None
) -> None:
    _off(prev)
    prev.fake.preview_errors = [_colour_error()]
    made = await generated(prev.world)
    (first,) = preview_jobs(prev, made.listing_id)
    await run_job(first["id"])

    url = f"/api/v1/listings/{made.listing_id}/fixes/apply"
    refused = await api.post(url, headers=bearer_for(prev.world, REVIEWER))
    assert refused.status_code == 403, refused.text
    response = await api.post(url, headers=bearer_for(prev.world, OWNER))
    assert response.status_code == 200, response.text
    assert response.json()["released"] == 1 and response.json()["job_id"] is not None
    assert _repairs(prev, made.listing_id) == [("enum_fold", "color", False, None)]
    jobs = preview_jobs(prev, made.listing_id)
    assert len(jobs) == 2 and jobs[1]["payload_json"]["trigger"] == "resolution"
    ((_path, status, resolution, action),) = _issue_status(prev, made.listing_id)
    assert (status, resolution, action) == ("auto_fixed", "deterministic_fix", "repair:enum_fold")
    assert _audit(prev, "listing_repair.approved") == [("user", prev.world.user(OWNER))]

    again = await api.post(url, headers=bearer_for(prev.world, OWNER))
    assert again.status_code == 409, again.text  # nothing left open to apply

    prev.fake.preview_errors = []
    await run_job(jobs[1]["id"])
    assert prev.listing(made.listing_id)["status"] == "validated"


async def test_on_is_unchanged(prev: PubWorld, marketplace_enum: None) -> None:
    prev.fake.preview_errors = [_colour_error()]
    made = await generated(prev.world)
    (first,) = preview_jobs(prev, made.listing_id)
    await run_job(first["id"])
    assert _repairs(prev, made.listing_id) == [("enum_fold", "color", False, None)]
    assert len(preview_jobs(prev, made.listing_id)) == 2
    assert _audit(prev, "listing_repair.suggested") == []


def _apply_url(listing_id: object) -> str:
    return f"/api/v1/listings/{listing_id}/fixes/apply"


@pytest.mark.endpoint("listings_fixes_apply", case="validation")
async def test_apply_bad_uuid_422(api: httpx.AsyncClient, prev: PubWorld) -> None:
    response = await api.post(_apply_url("nope"), headers=bearer_for(prev.world, OWNER))
    assert response.status_code == 422


@pytest.mark.endpoint("listings_fixes_apply", case="missing_auth")
async def test_apply_missing_auth_401(api: httpx.AsyncClient, prev: PubWorld) -> None:
    assert (await api.post(_apply_url(uuid.uuid4()))).status_code == 401


async def _invalid(api: httpx.AsyncClient, pw: PubWorld, variant: str) -> None:
    response = await api.post(
        _apply_url(uuid.uuid4()), headers=bad_bearer(pw.world, variant, OWNER)
    )
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


@pytest.mark.endpoint("listings_fixes_apply", case="invalid_token", variant="expired")
async def test_apply_invalid_token_expired(api: httpx.AsyncClient, prev: PubWorld) -> None:
    await _invalid(api, prev, "expired")


@pytest.mark.endpoint("listings_fixes_apply", case="invalid_token", variant="bad_signature")
async def test_apply_invalid_token_bad_signature(api: httpx.AsyncClient, prev: PubWorld) -> None:
    await _invalid(api, prev, "bad_signature")


@pytest.mark.endpoint("listings_fixes_apply", case="invalid_token", variant="wrong_typ")
async def test_apply_invalid_token_wrong_typ(api: httpx.AsyncClient, prev: PubWorld) -> None:
    await _invalid(api, prev, "wrong_typ")


@pytest.mark.endpoint("listings_fixes_apply", case="wrong_tenant")
async def test_apply_other_tenant_404(
    api: httpx.AsyncClient, prev: PubWorld, marketplace_enum: None
) -> None:
    _off(prev)
    prev.fake.preview_errors = [_colour_error()]
    made = await generated(prev.world)
    (first,) = preview_jobs(prev, made.listing_id)
    await run_job(first["id"])
    response = await api.post(
        _apply_url(made.listing_id), headers=prev.world.headers("operator@bharat.test")
    )
    assert response.status_code == 404
    assert _repairs(prev, made.listing_id) == [("enum_fold", "color", True, None)]


@pytest.mark.endpoint("listings_fixes_apply", case="not_found")
async def test_apply_not_found_404(api: httpx.AsyncClient, prev: PubWorld) -> None:
    response = await api.post(_apply_url(uuid.uuid4()), headers=bearer_for(prev.world, OWNER))
    assert response.status_code == 404


async def test_off_keeps_a_content_fix_as_a_suggestion_until_applied(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    """A title Amazon refused as too long: with auto-fix off Kuinn starts no content fix job,
    the reads say the seller applies it (never "Kuinn's team is looking"), and "Apply fixes"
    starts the fix job."""
    from mkos_schemas.codes import ErrorCode

    from .publish_support import approved, publish_and_run
    from .test_si11b_attacks import _content, _gen_jobs, _read

    _off(prev)
    prev.fake.preview_errors = []
    made = await approved(prev)
    title = _content(prev, made.version_id)["content"]["title"]
    prev.fake.set_fail_fields(
        {
            "content.title": (
                "99301",
                f"The value for 'item_name' is longer than {len(title) - 3} characters.",
                ErrorCode.FIELD_TOO_LONG,
            )
        }
    )
    await publish_and_run(api, prev, made)
    assert _gen_jobs(prev, made.listing_id, "auto_fix") == []  # nothing ran by itself

    auth = bearer_for(prev.world, OWNER)
    history = await api.get(f"/api/v1/listings/{made.listing_id}/issue-history", headers=auth)
    assert history.status_code == 200, history.text
    shown = [i for i in history.json()["issues"] if i["field"] == "content.title"]
    assert shown and all(
        (i["suggested"], i["handler"], i["lane"]) == (True, "you", "seller") for i in shown
    ), shown
    listed = await api.get(
        "/api/v1/issues", params={"listing_id": str(made.listing_id)}, headers=auth
    )
    assert listed.status_code == 200, listed.text
    inbox = {i["lane"] for i in listed.json()["data"] if i["field_path"] == "content.title"}
    assert inbox == {"seller"}, inbox
    assert (await _read(api, prev, made.listing_id))["state"] != "fixing"

    response = await api.post(_apply_url(made.listing_id), headers=auth)
    assert response.status_code == 200, response.text
    (job,) = _gen_jobs(prev, made.listing_id, "auto_fix")
    assert response.json()["job_id"] == str(job["id"])


async def test_turning_it_back_on_releases_the_held_repair(
    prev: PubWorld, marketplace_enum: None
) -> None:
    """A repair held while auto-fix was off goes live on the next run once the owner turns it
    on again: never refused as a second live repair (a staff `repair_rejected`)."""
    from app.repositories.uow import tenant_uow
    from app.services.resolution.run import resolve

    _off(prev)
    prev.fake.preview_errors = [_colour_error()]
    made = await generated(prev.world)
    (first,) = preview_jobs(prev, made.listing_id)
    await run_job(first["id"])
    assert _repairs(prev, made.listing_id) == [("enum_fold", "color", True, None)]

    prev.conn.execute(
        "UPDATE tenants SET auto_fix_enabled = true WHERE id = %s", [prev.world.tenant("acme")]
    )
    async with tenant_uow(prev.world.tenant("acme")) as uow:
        listing = await uow.listings.get(made.listing_id)
        job = await uow.job_runs.get_by_id(first["id"])
        assert job is not None
        result = await resolve(uow, listing, entry="preview", job=job)
    assert result is not None
    assert [s.lane for s in result.steps] == ["auto_repair"]
    assert _repairs(prev, made.listing_id) == [("enum_fold", "color", False, None)]
    assert _audit(prev, "listing_repair.approved") == [("system", None)]
    assert len(preview_jobs(prev, made.listing_id)) == 2  # the re-check follows
