"""`images recheck` (photo rules 2.1.0): the tenant by id, slug or seller id. Bad arguments exit
2 before any connection opens; the slug and the seller id resolve as `ops amazon-check` does, and
a seller id is never printed whole."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app import cli

TENANT = uuid.uuid4()
SELLER = "A1CHECKSELLER9"


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[tuple[uuid.UUID, str | None]]:
    calls: list[tuple[uuid.UUID, str | None]] = []

    async def recheck(tenant_id: uuid.UUID, staff_email: str | None = None) -> int:
        calls.append((tenant_id, staff_email))
        return cli.EXIT_OK

    monkeypatch.setattr(cli, "run_images_recheck", recheck)
    return calls


def _no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(*args: object, **kwargs: object) -> Any:
        raise AssertionError("no lookup may run")

    monkeypatch.setattr(cli, "_tenant_by_slug", refuse)
    monkeypatch.setattr(cli, "_tenants_by_seller", refuse)


@pytest.mark.parametrize(
    "argv",
    [
        ["images", "recheck"],
        ["images", "recheck", "--staff-email", "ops@x.test"],
        ["images", "recheck", "--tenant", "not-a-uuid"],
        ["images", "recheck", "--tenant", str(TENANT), "--tenant-slug", "acme"],
        ["images", "recheck", "--tenant-slug", "acme", "--seller-id", SELLER],
        ["images", "recheck", "--seller-id", "a1lower"],
        ["images", "recheck", "--tenant", str(TENANT), "--staff-email", "not-an-email"],
    ],
)
def test_bad_arguments_exit_2_before_any_connection(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    seen: list[tuple[uuid.UUID, str | None]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_db(monkeypatch)
    with pytest.raises(SystemExit) as caught:
        cli.main(argv)
    assert caught.value.code == 2
    assert seen == []
    assert SELLER not in capsys.readouterr().err


def test_tenant_id_runs_without_a_lookup(
    monkeypatch: pytest.MonkeyPatch, seen: list[tuple[uuid.UUID, str | None]]
) -> None:
    _no_db(monkeypatch)
    assert cli.main(["images", "recheck", "--tenant", str(TENANT)]) == 0
    assert cli.main(["images", "recheck", f"--tenant={TENANT}", "--staff-email=ops@x.test"]) == 0
    assert seen == [(TENANT, None), (TENANT, "ops@x.test")]


def test_slug_resolves_then_runs(
    monkeypatch: pytest.MonkeyPatch,
    seen: list[tuple[uuid.UUID, str | None]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def by_slug(slug: str) -> uuid.UUID | None:
        return TENANT if slug == "acme" else None

    monkeypatch.setattr(cli, "_tenant_by_slug", by_slug)
    argv = ["images", "recheck", "--tenant-slug", "acme", "--staff-email", "ops@x.test"]
    assert cli.main(argv) == 0
    assert seen == [(TENANT, "ops@x.test")]
    assert f"images recheck: tenant acme, id {TENANT}" in capsys.readouterr().out
    assert cli.main(["images", "recheck", "--tenant-slug", "nope"]) == cli.EXIT_NOT_ELIGIBLE
    assert "images recheck: no such tenant" in capsys.readouterr().err
    assert len(seen) == 1


def test_seller_id_resolves_masked_then_runs(
    monkeypatch: pytest.MonkeyPatch,
    seen: list[tuple[uuid.UUID, str | None]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def by_seller(seller_id: str) -> list[tuple[uuid.UUID, str]]:
        return [(TENANT, "acme")] if seller_id == SELLER else []

    monkeypatch.setattr(cli, "_tenants_by_seller", by_seller)
    assert cli.main(["images", "recheck", "--seller-id", SELLER, "--staff-email=o@x.test"]) == 0
    out = capsys.readouterr().out
    assert f"images recheck: tenant acme (seller A1C...ER9), id {TENANT}" in out
    assert SELLER not in out
    assert seen == [(TENANT, "o@x.test")]


@pytest.mark.parametrize(
    ("matches", "message"),
    [
        ([], "images recheck: no tenant has seller A1C...ER9 connected"),
        (
            [(TENANT, "acme"), (uuid.uuid4(), "beta")],
            "images recheck: 2 tenants have seller A1C...ER9 connected; pass --tenant-slug instead",
        ),
    ],
)
def test_seller_id_must_match_exactly_one_tenant(
    monkeypatch: pytest.MonkeyPatch,
    seen: list[tuple[uuid.UUID, str | None]],
    capsys: pytest.CaptureFixture[str],
    matches: list[tuple[uuid.UUID, str]],
    message: str,
) -> None:
    async def by_seller(seller_id: str) -> list[tuple[uuid.UUID, str]]:
        return matches

    monkeypatch.setattr(cli, "_tenants_by_seller", by_seller)
    assert cli.main(["images", "recheck", "--seller-id", SELLER]) == cli.EXIT_NOT_ELIGIBLE
    captured = capsys.readouterr()
    assert message in captured.err
    assert SELLER not in captured.out + captured.err
    assert seen == []


def test_a_lookup_failure_names_the_class_only(
    monkeypatch: pytest.MonkeyPatch,
    seen: list[tuple[uuid.UUID, str | None]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def broken(seller_id: str) -> list[tuple[uuid.UUID, str]]:
        raise RuntimeError(f"connection to db failed for {seller_id} password=hunter2")

    monkeypatch.setattr(cli, "_tenants_by_seller", broken)
    assert cli.main(["images", "recheck", "--seller-id", SELLER]) == cli.EXIT_FAILED
    err = capsys.readouterr().err
    assert "images recheck: failed (RuntimeError" in err
    assert SELLER not in err and "hunter2" not in err
    assert seen == []
