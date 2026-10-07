"""S14b T20 (AC 12; Done-when 5): the admin renders the seeded listing's `content_json` and its
rules report, and no page of any view ever holds a `token_enc` or `hashed_password` value."""

from __future__ import annotations

import base64
import re
import uuid
from typing import Any

import pytest

from .admin_support import ADMIN, AdminClient
from .conftest import LOCAL_HOSTS
from .seam_support import Conn

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
TOKEN = b"S14B-token-enc-sentinel-\x00\x01\x02\xfe\xff-bytes"


@pytest.fixture
async def seeded(auth_db: Conn) -> Conn:
    from app.cli import run_seed
    from app.db.seed_ops import seed_tenant_id

    from . import factories as f

    assert await run_seed("demo", reset=False) == 0
    f.marketplace_account(auth_db, seed_tenant_id("acme"), token_enc=TOKEN)
    return auth_db


def _secrets(conn: Conn) -> list[str]:
    hashes = [r[0] for r in conn.execute("SELECT hashed_password FROM users").fetchall() if r[0]]
    assert hashes
    forms = [
        TOKEN.decode("latin-1"),
        TOKEN.hex(),
        "\\x" + TOKEN.hex(),
        base64.b64encode(TOKEN).decode(),
        base64.urlsafe_b64encode(TOKEN).decode(),
        TOKEN[:24].decode("latin-1"),
    ]
    return forms + hashes


def _counts(conn: Conn) -> dict[str, int]:
    from app.admin.views import ADMIN_VIEWS

    out = {}
    for view in ADMIN_VIEWS:
        row = conn.execute(f"SELECT count(*) FROM {view.table}").fetchone()  # noqa: S608
        assert row is not None
        out[view.table] = int(row[0])
    return out


def identity_of(table: str) -> str:
    from app.admin.views import ADMIN_VIEWS

    (view,) = [v for v in ADMIN_VIEWS if v.table == table]
    return str(view.identity)


def _detail_links(html: str, identity: str) -> list[str]:
    found = re.findall(rf"{ADMIN}/{identity}/details/([0-9a-f-]{{36}})", html)
    return sorted(set(found))


async def test_admin_read_only_listing_version_shows_content_title(
    admin_client: AdminClient, seeded: Conn
) -> None:
    from app.db.seed_ops import DEMO_TITLE, demo_listing_ids

    _, version_id, _ = demo_listing_ids()
    async with admin_client() as client:
        response = await client.get(
            f"{ADMIN}/{identity_of('listing_versions')}/details/{version_id}"
        )
    assert response.status_code == 200, response.text
    assert DEMO_TITLE in response.text


async def test_admin_read_only_validation_run_shows_field_too_long(
    admin_client: AdminClient, seeded: Conn
) -> None:
    from app.db.seed_ops import demo_listing_ids

    _, _, run_id = demo_listing_ids()
    async with admin_client() as client:
        response = await client.get(f"{ADMIN}/{identity_of('validation_runs')}/details/{run_id}")
    assert response.status_code == 200, response.text
    assert "FIELD_TOO_LONG" in response.text
    assert "content.title" in response.text


async def test_admin_read_only_crawl_no_secrets(admin_client: AdminClient, seeded: Conn) -> None:
    from app.admin.views import view_identities

    secrets = _secrets(seeded)
    bodies: list[tuple[str, str]] = []
    async with admin_client() as client:
        for identity in view_identities():
            listed = await client.get(f"{ADMIN}/{identity}/list?pageSize=100")
            assert listed.status_code == 200, identity
            bodies.append((str(listed.url), listed.text))
            links = _detail_links(listed.text, identity)
            wanted = {identity_of(t) for t in ("marketplace_accounts", "users", "listing_versions")}
            if identity in wanted:
                assert links, identity  # the crawl reaches the rows that matter
            for pk in links:
                detail = await client.get(f"{ADMIN}/{identity}/details/{pk}")
                assert detail.status_code == 200, (identity, pk)
                bodies.append((str(detail.url), detail.text))
    assert len(bodies) > 12
    for url, body in bodies:
        leaked = [s for s in secrets if s in body]
        assert leaked == [], url


@pytest.mark.parametrize("action", ["create", "edit", "delete", "export"])
async def test_admin_read_only_writes_refused(
    admin_client: AdminClient, seeded: Conn, action: str
) -> None:
    from app.db.seed_ops import demo_listing_ids

    listing_id = demo_listing_ids()[0]
    before = _counts(seeded)
    requests: list[tuple[str, str]] = {
        "create": [
            ("GET", f"{ADMIN}/{identity_of('listings')}/create"),
            ("POST", f"{ADMIN}/{identity_of('listings')}/create"),
        ],
        "edit": [
            ("GET", f"{ADMIN}/{identity_of('listings')}/edit/{listing_id}"),
            ("POST", f"{ADMIN}/{identity_of('listings')}/edit/{listing_id}"),
        ],
        "delete": [("DELETE", f"{ADMIN}/{identity_of('listings')}/delete?pks={listing_id}")],
        "export": [
            ("GET", f"{ADMIN}/{identity_of('listings')}/export/csv"),
            ("GET", f"{ADMIN}/{identity_of('users')}/export/json"),
        ],
    }[action]
    async with admin_client() as client:
        for method, path in requests:
            response = await client.request(method, path, data={"status": "published"})
            assert 400 <= response.status_code < 500, (method, path, response.status_code)
    assert _counts(seeded) == before
    status = seeded.execute("SELECT status FROM listings WHERE id = %s", [listing_id]).fetchone()
    assert status == ("created",)


async def test_admin_read_only_unknown_pk_404(admin_client: AdminClient, seeded: Conn) -> None:
    async with admin_client() as client:
        response = await client.get(f"{ADMIN}/{identity_of('listings')}/details/{uuid.uuid4()}")
        garbage = await client.get(f"{ADMIN}/{identity_of('listings')}/details/not-a-uuid")
    assert response.status_code == 404
    assert 400 <= garbage.status_code < 500


@pytest.mark.parametrize("query", ["page=-1", "page=abc", "sort=bogus", "pageSize=0", "page=99999"])
async def test_admin_read_only_bad_page_sort_not_500(
    admin_client: AdminClient, seeded: Conn, query: str
) -> None:
    async with admin_client() as client:
        response = await client.get(f"{ADMIN}/{identity_of('listings')}/list?{query}")
    assert response.status_code < 500, response.status_code


def test_admin_read_only_secret_forms_cover_bytes() -> None:
    forms: list[Any] = [TOKEN.hex(), base64.b64encode(TOKEN).decode()]
    assert all(forms)
