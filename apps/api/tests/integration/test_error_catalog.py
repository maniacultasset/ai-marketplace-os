"""SI11 T19 (AC13): the marketplace error catalogue, staff only. An unknown code answered on two
tenants is one unmapped row (`tenants 2`) with a suggested map row, in `errors catalog
--unmapped` and on the ops page; neither shows a tenant id or any message text."""

from __future__ import annotations

import json
from typing import Any

import pytest
from app import cli

from . import factories as f
from .admin_support import ADMIN, AdminClient
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .review_support import generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
SECRET = "saffron-zari-7731 refused for tenant reasons"


async def _unknown_on_two_tenants(world: GenWorld) -> list[Any]:
    made = [await generated(world), await generated(world, "operator@bharat.test")]
    for slug, listing in zip(("acme", "bharat"), made, strict=True):
        f.listing_issue(
            world.conn,
            world.tenant(slug),
            listing.listing_id,
            source="amazon_adapter",
            field_path="listing",
            error_code="MARKETPLACE_UNKNOWN_ERROR",
            error_class="unknown",
            retry_class="user_action",
            marketplace_code="77777",
            message=SECRET,
            raw_message=SECRET,
            dedupe_key=f"amazon_adapter|listing|MARKETPLACE_UNKNOWN_ERROR|77777|{slug}",
        )
    return made


async def test_the_cli_lists_the_unmapped_code_with_a_suggested_row(
    gen_world: GenWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    await _unknown_on_two_tenants(gen_world)
    assert await cli.run_errors_catalog(unmapped=True, limit=50) == 0
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines() if line.startswith('{"')]
    (row,) = [r for r in lines if r.get("marketplace_code") == "77777"]
    assert (row["mapped"], row["tenants"], row["issues"], row["lane"]) == (False, 2, 2, "staff")
    suggested = row["suggested_row"]
    assert suggested["raw_value"] == "77777" and suggested["policy_ref"]["observed"] is True
    assert suggested["fix_action"] is None
    assert SECRET not in out
    for slug in ("acme", "bharat"):
        assert str(gen_world.tenant(slug)) not in out


async def test_the_ops_page_lists_it_for_staff(
    gen_world: GenWorld, admin_client: AdminClient
) -> None:
    await _unknown_on_two_tenants(gen_world)
    async with admin_client() as client:
        response = await client.get(f"{ADMIN}/ops/errors")
    assert response.status_code == 200, response.text
    html = response.text.split('data-ops="errors"', 1)[1]
    assert "77777" in html and "&#34;raw_value&#34;: &#34;77777&#34;" in html
    assert SECRET not in response.text
    for slug in ("acme", "bharat"):
        assert str(gen_world.tenant(slug)) not in response.text


async def test_the_ops_page_is_refused_outside_local(admin_client: AdminClient) -> None:
    async with admin_client("staging") as client:
        response = await client.get(f"{ADMIN}/ops/errors")
    assert response.status_code in (403, 404)


def test_the_catalog_limit_is_bounded() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["errors", "catalog"]).limit == 200
    for bad in ("0", "501", "x"):
        with pytest.raises(SystemExit):
            parser.parse_args(["errors", "catalog", "--limit", bad])
