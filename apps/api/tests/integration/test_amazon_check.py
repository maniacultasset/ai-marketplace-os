"""The read-only Amazon connection check (`app.workers.amazon_check`, `app.cli ops amazon-check`)
against the in-process fake marketplace as `app_rw`: a connected account walks every step, only
GET requests reach SP-API (the LWA refresh is the one POST), the account row is untouched, one
audit row names the staff actor, and no token reaches the table or the logs."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import httpx
import pytest
from app import cli
from tests.adapters.amazon_support import CountingLimiter
from tests.conftest import LogCapture
from tests.support.fake_marketplace import catalog_asin, ptd_fixture

from . import ptd_support
from .auth_support import audit_rows
from .conftest import LOCAL_HOSTS
from .marketplace_support import MktWorld, account_rows, connected_account, mapi, mkt

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["mapi", "mkt"]
SELLER = "A1CHECKSELLER9"
STAFF = "Staff@Example.com"
EAN = "8901234567890"
CANARY = "CANARYzq9"
SANDBOX = "https://sandbox.sellingpartnerapi-eu.amazon.com"


@pytest.fixture
def deps(mkt: MktWorld, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The check over the fake: SP-API and the schema link on the ASGI app, a public address for
    the link host, and a limiter that never waits."""
    from app.core.config import get_settings
    from app.workers import amazon_check

    async def resolve(host: str) -> Sequence[str]:
        return ["52.95.150.10"]

    def make() -> amazon_check.CheckDeps:
        return amazon_check.CheckDeps(
            transport=httpx.ASGITransport(mkt.fake), resolver=resolve, limiter=CountingLimiter()
        )

    monkeypatch.setenv("AMAZON_SPAPI_ENDPOINT", SANDBOX)
    get_settings.cache_clear()
    monkeypatch.setattr(amazon_check, "make_check_deps", make)
    yield make
    get_settings.cache_clear()


@pytest.fixture
async def account(mkt: MktWorld, mapi: httpx.AsyncClient, deps: Any) -> dict[str, Any]:
    body = await connected_account(mapi, mkt, seller=SELLER)
    mkt.control.calls.clear()
    mkt.control.token_requests.clear()
    return body


def _listing_schema(product_type: str) -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(ptd_fixture(f"{product_type}.listing.schema.json"))
    return schema


async def _check(mkt: MktWorld, **kwargs: Any) -> Any:
    from app.workers.amazon_check import run_check

    return await run_check(mkt.tenant("acme"), staff_email="staff@example.com", **kwargs)


def _rows(report: Any) -> dict[str, tuple[str, str]]:
    return {row.step: (row.result, row.detail) for row in report.rows}


def _sp_methods(mkt: MktWorld) -> list[str]:
    return [str(call["method"]) for call in mkt.control.calls]


def _no_secret(*texts: str) -> None:
    for text in texts:
        assert CANARY not in text
        assert SELLER not in text  # the seller id is masked
        assert "X-Amz-" not in text and "__ptd_schema" not in text


async def test_full_check_reads_only_and_leaks_nothing(
    mkt: MktWorld, account: dict[str, Any], log_lines: Callable[..., LogCapture]
) -> None:
    capture = log_lines()
    ptd_support.store(mkt.conn, _listing_schema("KURTA"), version="UFAKE0001")
    mkt.control.spapi_catalog[EAN] = {
        "scenario": "same_brand_ean",
        "restriction": "brand_restricted",
    }
    before = next(r for r in account_rows(mkt.conn) if str(r["id"]) == account["id"])

    report = await _check(mkt, ean=EAN)

    rows = _rows(report)
    assert [row.step for row in report.rows] == [
        "0 config",
        "1 account",
        "2 lwa",
        "3 participations",
        "4 ptd KURTA",
        "4 ptd SHIRT",
        "5 listings",
        "6 catalog",
        "7 restrictions",
        "8 rate limits",
    ]
    assert {step: result for step, (result, _) in rows.items()} == {
        "0 config": "PASS",
        "1 account": "PASS",
        "2 lwa": "PASS",
        "3 participations": "PASS",
        "4 ptd KURTA": "PASS",
        "4 ptd SHIRT": "WARN",  # no stored SHIRT definition
        "5 listings": "PASS",
        "6 catalog": "PASS",
        "7 restrictions": "PASS",
        "8 rate limits": "PASS",
    }
    assert (
        "seller A1C...ER9" in rows["1 account"][1]
        and "refresh token present" in rows["1 account"][1]
    )
    assert "Amazon.in (A21TJRUUN4KGV): participating, active" in rows["3 participations"][1]
    assert "version UFAKE0001" in rows["4 ptd KURTA"][1] and "in sync" in rows["4 ptd KURTA"][1]
    assert "no stored definition" in rows["4 ptd SHIRT"][1]
    assert rows["6 catalog"][1] == f"1 item(s) for EAN {EAN}"
    asin = catalog_asin(EAN)
    assert rows["7 restrictions"][1] == f"1 restriction(s) for ASIN {asin} (APPROVAL_REQUIRED)"
    assert "sellers 0.016/s" in rows["8 rate limits"][1]
    assert report.result == "WARN" and report.passed
    assert str(report.account_id) == account["id"]

    # only GET reached SP-API (the PTD link download included), and the one POST was the refresh
    assert report.refused == 0 and report.gets == len(mkt.control.calls) == 8
    assert set(_sp_methods(mkt)) == {"GET"}
    assert [r["grant_type"] for r in mkt.control.token_requests] == ["refresh_token"]
    assert mkt.control.spapi_items == {} and mkt.control.spapi_preview_calls == 0
    # the account row is untouched: the fresh token was never stored
    after = next(r for r in account_rows(mkt.conn) if str(r["id"]) == account["id"])
    assert bytes(after["token_enc"]) == bytes(before["token_enc"])
    assert after["updated_at"] == before["updated_at"] and after["status"] == "active"
    # one audit row names the staff actor
    [audit] = audit_rows(mkt.conn, "marketplace_account.connection_check")
    assert audit[1] == "staff" and audit[3] == "staff@example.com"
    assert str(audit[5]) == account["id"] and audit[9] == "amazon_connection_check"

    _no_secret(report.render(), capture.text())
    done = [line for line in capture.lines() if line["message"] == "amazon_check.done"]
    assert len(done) == 1 and done[0]["result"] == "WARN" and done[0]["refused"] == 0


async def test_stale_definition_is_a_warning(mkt: MktWorld, account: dict[str, Any]) -> None:
    ptd_support.store(mkt.conn, _listing_schema("KURTA"), version="UOLD0001")
    report = await _check(mkt)
    result, detail = _rows(report)["4 ptd KURTA"]
    assert result == "WARN"
    assert "version UFAKE0001" in detail and "stored version UOLD0001 is stale" in detail
    # the default EAN matched nothing in the fake: restrictions are skipped, not failed
    assert _rows(report)["6 catalog"] == ("PASS", "0 item(s) for EAN 5449000000996")
    assert _rows(report)["7 restrictions"][0] == "SKIP"
    assert report.passed


async def test_refused_refresh_fails_and_changes_nothing(
    mkt: MktWorld, account: dict[str, Any]
) -> None:
    mkt.control.next_refresh = "invalid_grant"
    report = await _check(mkt)
    rows = _rows(report)
    assert rows["2 lwa"] == (
        "FAIL",
        "Amazon refused the refresh token (invalid_grant): reconnect",
    )
    assert {rows[s][0] for s in rows if s[0] in "345678"} == {"SKIP"}
    assert not report.passed and mkt.control.calls == []
    # a diagnostic never moves the account to needs_reconnect (the refresh job owns that)
    row = next(r for r in account_rows(mkt.conn) if str(r["id"]) == account["id"])
    assert row["status"] == "active"


async def test_not_participating_fails(mkt: MktWorld, account: dict[str, Any]) -> None:
    mkt.control.spapi_participations = "not_participating"
    report = await _check(mkt)
    assert _rows(report)["3 participations"] == (
        "FAIL",
        "Amazon.in (A21TJRUUN4KGV): not participating",
    )
    assert not report.passed
    mkt.control.spapi_participations = "other_only"
    report = await _check(mkt)
    assert _rows(report)["3 participations"][0] == "FAIL"
    mkt.control.spapi_participations = "suspended"
    report = await _check(mkt)
    assert _rows(report)["3 participations"][0] == "WARN"


async def test_denied_reads_fail_by_class_only(mkt: MktWorld, account: dict[str, Any]) -> None:
    mkt.control.spapi_auth_mode, mkt.control.spapi_auth_times = "denied", 1
    report = await _check(mkt)
    assert _rows(report)["5 listings"] == ("FAIL", "PermissionDenied (ACCOUNT_PERMISSION_DENIED)")
    _no_secret(report.render())


async def test_a_write_in_a_step_is_refused_before_it_is_sent(
    mkt: MktWorld, account: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prove the guard holds in the real run: a step that tried to write would fail closed."""
    from app.workers import amazon_check

    async def writes(self: Any) -> Any:
        sp, _ = self.clients()
        await sp.put("SKU-1", {"productType": "KURTA", "attributes": {}})
        raise AssertionError("unreachable")

    monkeypatch.setattr(amazon_check._Run, "listings", writes)
    report = await _check(mkt)
    assert _rows(report)["5 listings"] == (
        "FAIL",
        "refused a request that is not a GET (nothing was sent)",
    )
    assert report.refused == 1 and report.result == "FAIL"
    assert set(_sp_methods(mkt)) == {"GET"} and mkt.control.spapi_items == {}


async def test_no_account_fails_without_an_audit(mkt: MktWorld, deps: Any) -> None:
    report = await _check(mkt)
    rows = _rows(report)
    assert rows["1 account"] == ("FAIL", "no Amazon account is connected for this tenant")
    assert {rows[s][0] for s in rows if s[0] in "2345678"} == {"SKIP"}
    assert audit_rows(mkt.conn, "marketplace_account.connection_check") == []
    assert mkt.control.token_requests == []


async def test_worker_entrypoint_prints_the_table(
    mkt: MktWorld, account: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    from app.workers import amazon_check

    argv = ["--tenant-id", str(mkt.tenant("acme")), "--staff-email", STAFF, "--ean", EAN]
    code = await amazon_check._main(amazon_check.build_parser().parse_args(argv))
    out = capsys.readouterr()
    assert code == 0, out.err
    # stdout also carries the structured log lines (JSON); the table is the rest
    lines = [line for line in out.out.splitlines() if not line.startswith("{")]
    assert lines[0] == f"amazon check: tenant {mkt.tenant('acme')} account {account['id']}"
    assert lines[1].split() == ["STEP", "RESULT", "TIME", "DETAIL"]
    # no stored KURTA or SHIRT definition (two warnings); the EAN matches nothing, so no ASIN
    assert lines[-1] == (
        "amazon check: WARN (7 pass, 2 warn, 0 fail, 1 skip); "
        "requests: 7 GET, 0 other refused, 1 LWA token refresh"
    )
    _no_secret(out.out, out.err)
    [audit] = audit_rows(mkt.conn, "marketplace_account.connection_check")
    assert audit[3] == "staff@example.com"


async def test_worker_entrypoint_fails_on_a_failed_step(
    mkt: MktWorld, account: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    from app.workers import amazon_check

    mkt.control.spapi_participations = "not_participating"
    argv = ["--tenant-id", str(mkt.tenant("acme")), "--staff-email", STAFF]
    assert await amazon_check._main(amazon_check.build_parser().parse_args(argv)) == 1
    table = [line for line in capsys.readouterr().out.splitlines() if not line.startswith("{")]
    assert table[-1].startswith("amazon check: FAIL (")


async def test_cli_resolves_the_slug(mkt: MktWorld) -> None:
    assert await cli._tenant_by_slug("acme") == mkt.tenant("acme")
    assert await cli._tenant_by_slug("nobody") is None


async def test_cli_resolves_the_seller_id(mkt: MktWorld, account: dict[str, Any]) -> None:
    """The connected account's seller id finds its one tenant, with the tenant's slug."""
    assert await cli._tenants_by_seller(SELLER) == [(mkt.tenant("acme"), "acme")]
    assert await cli._tenants_by_seller("A1NOSUCHSELLER") == []


def test_cli_hands_over_by_seller_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--seller-id resolves exactly one tenant, prints its slug, never the whole seller id."""
    tenant = uuid.uuid4()
    seen: list[list[str]] = []
    found: dict[str, list[tuple[uuid.UUID, str]]] = {
        SELLER: [(tenant, "acme-x7k2")],
        "A1NOSUCHSELLER": [],
        "A1TWOTENANTS99": [(uuid.uuid4(), "a-one"), (uuid.uuid4(), "b-two")],
    }

    async def resolve(seller_id: str) -> list[tuple[uuid.UUID, str]]:
        return found[seller_id]

    monkeypatch.setattr(cli, "_tenants_by_seller", resolve)
    monkeypatch.setattr(cli, "EXEC", lambda path, argv: seen.append([path, *argv]))
    base = ["ops", "amazon-check", "--staff-email", STAFF]
    assert cli.main([*base, "--seller-id", SELLER]) == 0
    assert len(seen) == 1 and seen[0][4:6] == ["--tenant-id", str(tenant)]
    out = capsys.readouterr()
    assert out.out == f"amazon check: tenant acme-x7k2 (seller A1C...ER9), id {tenant}\n"
    assert SELLER not in out.out + out.err

    assert cli.main([*base, "--seller-id", "A1NOSUCHSELLER"]) == cli.EXIT_NOT_ELIGIBLE
    out = capsys.readouterr()
    assert out.err == "amazon check: no tenant has seller A1N...LER connected\n"
    assert cli.main([*base, "--seller-id", "A1TWOTENANTS99"]) == cli.EXIT_NOT_ELIGIBLE
    out = capsys.readouterr()
    assert out.err.startswith("amazon check: 2 tenants have seller A1T...S99 connected")
    assert "A1TWOTENANTS99" not in out.out + out.err
    assert len(seen) == 1


def test_cli_hands_over_to_the_worker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI never runs the check itself: it execs the worker entrypoint with the tenant id."""
    import sys

    tenant = uuid.uuid4()
    seen: list[list[str]] = []

    async def resolve(slug: str) -> uuid.UUID | None:
        return tenant if slug == "acme" else None

    monkeypatch.setattr(cli, "_tenant_by_slug", resolve)
    monkeypatch.setattr(cli, "EXEC", lambda path, argv: seen.append([path, *argv]))
    base = ["ops", "amazon-check", "--staff-email", STAFF]
    assert cli.main([*base, "--tenant-slug", "acme", "--ean", EAN, "--asin", "B0ABCDEF12"]) == 0
    assert seen == [
        [
            sys.executable,
            sys.executable,
            "-m",
            "app.workers.amazon_check",
            "--tenant-id",
            str(tenant),
            "--staff-email",
            "staff@example.com",
            "--ean",
            EAN,
            "--asin",
            "B0ABCDEF12",
        ]
    ]
    assert capsys.readouterr().out == f"amazon check: tenant acme, id {tenant}\n"
    assert cli.main([*base, "--tenant-slug", "nobody"]) == cli.EXIT_NOT_ELIGIBLE
    assert capsys.readouterr().err == "amazon check: no such tenant\n"
    assert len(seen) == 1


@pytest.mark.parametrize(
    "extra",
    [
        ["--tenant-slug", "acme", "--ean", "12AB"],
        ["--tenant-slug", "acme", "--asin", "b0lower000"],
        ["--tenant-slug", "Bad Slug"],
        ["--tenant-slug", "acme", "--seller-id", SELLER],
        ["--seller-id", "a1lower00000"],
        [],
    ],
)
def test_cli_refuses_bad_arguments(extra: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    base = ["ops", "amazon-check", "--staff-email", STAFF]
    with pytest.raises(SystemExit) as exited:
        cli.build_parser().parse_args([*base, *extra])
    assert exited.value.code == 2
    assert "a1lower00000" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--tenant-id", "acme", "--staff-email", STAFF],
        ["--tenant-id", "00000000-0000-0000-0000-000000000001", "--staff-email", "nope"],
        [
            "--tenant-id",
            "00000000-0000-0000-0000-000000000001",
            "--staff-email",
            STAFF,
            "--ean",
            "1",
        ],
    ],
)
def test_worker_entrypoint_refuses_bad_arguments(argv: list[str]) -> None:
    from app.workers import amazon_check

    with pytest.raises(SystemExit) as exited:
        amazon_check.build_parser().parse_args(argv)
    assert exited.value.code == 2
