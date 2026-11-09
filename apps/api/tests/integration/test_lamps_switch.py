"""SI14 T23 (AC11): `CATEGORY_LAMPS_ENABLED` gates only the creation of a new lamps listing. Off, a
generate for a lamps product with no listing is 422 at `category` and the requirements read is
facts only; an existing lamps listing still regenerates and its job runs. On, today's
behaviour."""

from __future__ import annotations

import uuid

import httpx
import pytest

from . import factories as f
from .assist_support import set_switch
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world

__all__ = ["gen_world"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
LAMPS = "home_decor_lamps"
OPERATOR = "operator@acme.test"


def _lamp(world: GenWorld) -> uuid.UUID:
    return f.product(world.conn, world.tenant("acme"), category=LAMPS, title="Brass table lamp")


async def _generate(api: httpx.AsyncClient, world: GenWorld, product: uuid.UUID) -> httpx.Response:
    return await api.post(
        "/api/v1/listings/generate",
        headers=world.headers(OPERATOR),
        json={"product_id": str(product), "marketplace": "amazon"},
    )


async def _run(job_id: str) -> None:
    from app.workers.tasks import register_bodies
    from app.workers.wrapper import run_job

    register_bodies()
    await run_job(job_id)


async def test_off_refuses_a_new_lamps_listing(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_switch(monkeypatch, "lamps", on=False)
    product = _lamp(gen_world)
    response = await _generate(api, gen_world, product)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"
    assert gen_world.count("listings", "product_id = %s", [product]) == 0


async def test_off_requirements_are_facts_only(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    hdrs = gen_world.headers(OPERATOR)
    set_switch(monkeypatch, "lamps", on=True)
    on = (
        await api.get("/api/v1/catalog/requirements", headers=hdrs, params={"category": LAMPS})
    ).json()
    set_switch(monkeypatch, "lamps", on=False)
    off = (
        await api.get("/api/v1/catalog/requirements", headers=hdrs, params={"category": LAMPS})
    ).json()
    unknown = (
        await api.get(
            "/api/v1/catalog/requirements", headers=hdrs, params={"category": "no_such_cat"}
        )
    ).json()
    assert (off["amazon"], off["flipkart"]) == (unknown["amazon"], unknown["flipkart"])
    assert set(on["amazon"]) > set(off["amazon"])  # the lamp template's keys are gone while off


async def test_off_an_existing_lamps_listing_still_regenerates(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_switch(monkeypatch, "lamps", on=True)
    product = _lamp(gen_world)
    first = await _generate(api, gen_world, product)
    assert first.status_code == 202, first.text
    await _run(first.json()["job_id"])
    listing_id = first.json()["listing_id"]
    set_switch(monkeypatch, "lamps", on=False)
    again = await api.post(
        f"/api/v1/listings/{listing_id}/regenerate", headers=gen_world.headers(OPERATOR), json={}
    )
    assert again.status_code == 202, again.text
    await _run(again.json()["job_id"])
    job = gen_world.conn.execute(
        "SELECT status FROM job_runs WHERE id = %s", [again.json()["job_id"]]
    ).fetchone()
    assert job is not None and job[0] == "succeeded"
    status = gen_world.listing(uuid.UUID(listing_id))["status"]
    assert status in {"validated", "needs_review", "blocked"}, status


async def test_on_is_todays_behaviour(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_switch(monkeypatch, "lamps", on=True)
    response = await _generate(api, gen_world, _lamp(gen_world))
    assert response.status_code == 202, response.text


async def test_other_categories_are_never_gated(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_switch(monkeypatch, "lamps", on=False)
    response = await _generate(api, gen_world, gen_world.product("acme", "KURTA-002"))
    assert response.status_code == 202, response.text


async def test_off_a_forced_generate_of_an_existing_lamps_listing_is_accepted(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_switch(monkeypatch, "lamps", on=True)
    product = _lamp(gen_world)
    first = await _generate(api, gen_world, product)
    await _run(first.json()["job_id"])
    set_switch(monkeypatch, "lamps", on=False)
    forced = await api.post(
        "/api/v1/listings/generate",
        headers=gen_world.headers(OPERATOR),
        json={"product_id": str(product), "marketplace": "amazon", "force_regenerate": True},
    )
    assert forced.status_code == 202, forced.text
    assert forced.json()["listing_id"] == first.json()["listing_id"]
