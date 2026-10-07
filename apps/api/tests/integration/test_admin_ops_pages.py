"""S14b T22 (AC 14): the ops pages render the T21 numbers, offer no requeue and escape every
stored string."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from .admin_support import ADMIN, NOW, AdminClient, OpsWorld
from .conftest import LOCAL_HOSTS
from .seam_support import Conn

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "admin" / "templates" / "ops"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.admin.ops_views import OpsView

    monkeypatch.setattr(OpsView, "clock", staticmethod(lambda: NOW))


def _cells(html: str, table: str) -> list[list[str]]:
    body = html.split(f'data-ops="{table}"', 1)[1].split("</table>", 1)[0]
    rows = re.findall(r"<tr>(.*?)</tr>", body, flags=re.S)
    cells = [[c.strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", r, flags=re.S)] for r in rows]
    return [c for c in cells if c]  # the header row holds <th> cells only


@pytest.mark.parametrize("page", ["queue", "cost", "funnel", "dlq"])
async def test_admin_ops_pages_render_golden_numbers(
    admin_client: AdminClient, ops_world: OpsWorld, page: str
) -> None:
    async with admin_client() as client:
        response = await client.get(f"{ADMIN}/ops/{page}")
    assert response.status_code == 200, response.text
    html = response.text
    if page == "queue":
        assert _cells(html, "queue") == [[ops_world.queue, "3", "600", "3"]]
    elif page == "cost":
        days = _cells(html, "daily-spend")
        assert days[-2] == ["2026-10-04", "0.350000", "acme: 0.300000, bharat: 0.050000"]
        assert days[-1] == ["2026-10-05", "0.400000", "bharat: 0.400000"]
        per = html.split('data-ops="per-listing"', 1)[1]
        assert "<dd>0.750000</dd>" in per and "<dd>1</dd>" in per
    elif page == "funnel":
        assert _cells(html, "funnel") == [["created", "2"], ["published", "1"]]
    else:
        rows = _cells(html, "dlq")
        assert len(rows) == 3 and all("(bharat)" in r[1] for r in rows)


async def test_admin_ops_no_requeue_action(admin_client: AdminClient, ops_world: OpsWorld) -> None:
    async with admin_client() as client:
        for page in ("queue", "cost", "funnel", "dlq"):
            response = await client.get(f"{ADMIN}/ops/{page}")
            assert "<form" not in response.text.split('data-ops="', 1)[1]
            posted = await client.post(f"{ADMIN}/ops/{page}")
            assert posted.status_code == 405
    from app.admin.ops_views import OpsView

    exposed = [getattr(m, "_methods", None) for m in vars(OpsView).values()]
    assert all(methods in (None, ["GET"]) for methods in exposed)


async def test_admin_ops_escapes_tenant_name(
    admin_client: AdminClient, ops_world: OpsWorld, auth_db: Conn
) -> None:
    hostile = "<script>alert('x')</script>"
    auth_db.execute(
        "UPDATE tenants SET name = %s WHERE id = %s", [hostile, ops_world.world.tenant("bharat")]
    )
    async with admin_client() as client:
        response = await client.get(f"{ADMIN}/ops/dlq")
    assert response.status_code == 200
    assert hostile not in response.text
    assert "&lt;script&gt;" in response.text


def test_admin_ops_templates_no_safe_filter() -> None:
    templates = sorted(TEMPLATES.glob("*.html"))
    assert len(templates) == 5
    for path in templates:
        text = path.read_text(encoding="utf-8")
        assert "|safe" not in text.replace(" ", ""), path.name
        assert "autoescape false" not in text, path.name
        assert "<form" not in text, path.name
