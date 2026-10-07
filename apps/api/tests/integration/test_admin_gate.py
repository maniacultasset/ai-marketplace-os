"""S14b T17 (AC 13, AC 4 admin leg; D9, D13): the admin mount and its staff gate."""

from __future__ import annotations

from typing import Any

import pytest

from .admin_support import ADMIN, AdminClient, external_refs
from .auth_support import World
from .conftest import LOCAL_HOSTS
from .products_support import bearer_for

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
STAGING_PATHS = (f"{ADMIN}/", f"{ADMIN}/statics/css/main.css", f"{ADMIN}/listing/list", ADMIN)


async def test_admin_gate_local_serves(admin_client: AdminClient) -> None:
    async with admin_client("local") as client:
        response = await client.get(f"{ADMIN}/")
    assert response.status_code == 200, response.text
    assert "text/html" in response.headers["content-type"]


async def test_admin_gate_staging_403_all_paths(admin_client: AdminClient) -> None:
    async with admin_client("staging") as client:
        for path in STAGING_PATHS:
            response = await client.get(path)
            assert response.status_code in (403, 404), path
            if path != ADMIN:
                assert response.status_code == 403, path


async def test_admin_gate_staging_tenant_admin_jwt_403(
    admin_client: AdminClient, products_world: World
) -> None:
    headers = bearer_for(products_world, "admin@acme.test")
    async with admin_client("staging") as client:
        response = await client.get(f"{ADMIN}/", headers=headers)
    assert response.status_code == 403


async def test_admin_gate_no_store_header(admin_client: AdminClient) -> None:
    async with admin_client("local") as client:
        ok = await client.get(f"{ADMIN}/")
        refused = await client.post(f"{ADMIN}/listing/create")
    async with admin_client("staging") as client:
        forbidden = await client.get(f"{ADMIN}/")
    for response in (ok, refused, forbidden):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"


async def test_admin_gate_logs_email_and_path(admin_client: AdminClient, log_lines: Any) -> None:
    async with admin_client("local") as client:
        capture = log_lines()
        await client.get(f"{ADMIN}/")
    lines = [line for line in capture.lines() if line["message"] == "admin.view"]
    assert len(lines) == 1
    assert lines[0]["path"] == f"{ADMIN}/"
    assert "staff_email" in lines[0]  # S11 redacts the value like every e-mail field


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_admin_gate_write_methods_405(admin_client: AdminClient, method: str) -> None:
    async with admin_client("local") as client:
        for path in (f"{ADMIN}/", f"{ADMIN}/listing/create", f"{ADMIN}/listing/delete"):
            response = await client.request(method, path)
            assert response.status_code == 405, (method, path)


async def test_admin_not_mounted_when_disabled(admin_client: AdminClient) -> None:
    async with admin_client("local", enabled=False) as client:
        response = await client.get(f"{ADMIN}/")
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NOT_FOUND"


async def test_admin_html_loads_no_external_assets(admin_client: AdminClient) -> None:
    from app.admin.views import OPS_PAGES

    async with admin_client("local") as client:
        index = await client.get(f"{ADMIN}/")
        assert index.status_code == 200
        pages = [index]
        from app.admin import views

        for identity in views.view_identities():
            pages.append(await client.get(f"{ADMIN}/{identity}/list"))
        for page in OPS_PAGES:
            pages.append(await client.get(f"{ADMIN}/{page}"))
    for page in pages:
        assert page.status_code == 200, page.url
        assert external_refs(page.text) == [], page.url
