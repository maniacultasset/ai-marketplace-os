"""ADR-0033: `templates refresh --via-tenant SLUG`, the PTD fetch through an allow-listed tenant's
stored Amazon connection. The CLI's guards, the hand-over to the fetch child and the store, and
the child's guards, token source and output, over the in-process fake marketplace (no database,
no KMS, no network, no subprocess)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from app import cli
from app.adapters.amazon import definitions
from app.adapters.oauth.base import OAuthRejectedError, TokenGrant
from app.core.config import Settings, get_settings, ptd_tenant_slugs
from app.core.crypto import TokenPair
from app.db import ptd_loads
from app.db.ptd_loads import Actor, DefinitionRow, StoreResult
from app.workers import ptd_tenant
from app.workers.ptd_tenant import Deps, Refused, Request, TenantAccount
from pydantic import SecretStr
from tests.support.fake_marketplace import build_fake, issue_token

TENANT_REFRESH = "test-only-tenant-refresh-" + "r" * 24
PLATFORM_REFRESH = "test-only-platform-refresh-" + "p" * 24
SELLER = "A3SELLERTEST9"
TENANT = uuid.UUID("00000000-0000-4000-8000-000000000001")
ACCOUNT = uuid.UUID("00000000-0000-4000-8000-000000000002")
DB = "postgresql+psycopg://app_rw:x@localhost:5612/marketplace_os"
MIGRATOR_DB = "postgresql+psycopg://migrator:x@localhost:5612/marketplace_os"
ENV = {
    "APP_ENV": "local",
    "APP_ROLE": "shell",
    "DATABASE_URL": DB,
    "ALEMBIC_DATABASE_URL": MIGRATOR_DB,
    "AMAZON_APP_ID": "amzn1.sp.solution.test",
    "AMAZON_CLIENT_ID": "amzn1.application-oa2-client.test",
    "AMAZON_CLIENT_SECRET": "test-only-client-secret",
    "AMAZON_SPAPI_ENDPOINT": "http://127.0.0.1:4120",
    "AMAZON_PTD_TENANT_SLUGS": "test",
    # set on purpose: the via-tenant path must never use it
    "AMAZON_PLATFORM_REFRESH_TOKEN": PLATFORM_REFRESH,
}


def _settings(**over: Any) -> Settings:
    values: dict[str, Any] = {key.lower(): value for key, value in ENV.items()}
    values.update(over)
    return Settings(_env_file=None, **values)


def _request(tmp_path: Path, **over: Any) -> Request:
    values: dict[str, Any] = {
        "tenant_id": TENANT,
        "slug": "test",
        "product_type": "KURTA",
        "requirements": "LISTING",
        "staff_email": "admin@example.com",
        "reason": "beta style",
        "out": tmp_path / "definition.json",
    }
    values.update(over)
    return Request(**values)


class FakeOAuth:
    """The LWA refresh: records the refresh token it got and returns a token the fake accepts."""

    def __init__(self, app: Any, *, refuse: bool = False) -> None:
        self.app = app
        self.refuse = refuse
        self.seen: list[str] = []
        self.issued: list[str] = []

    async def refresh(self, refresh_token: SecretStr) -> TokenGrant:
        self.seen.append(refresh_token.get_secret_value())
        if self.refuse:
            raise OAuthRejectedError
        access = issue_token(self.app)
        self.issued.append(access)
        return TokenGrant(TokenPair(SecretStr(access), refresh_token), None, (), None, None)


@dataclass
class Child:
    """The fetch child's seams over the fake marketplace."""

    app: Any = field(default_factory=build_fake)
    account: TenantAccount | Exception | None = None
    oauth: FakeOAuth | None = None
    loads: int = 0

    def __post_init__(self) -> None:
        self.oauth = self.oauth or FakeOAuth(self.app)

    def deps(self) -> Deps:
        async def load_account(settings: Settings, request: Request) -> TenantAccount:
            self.loads += 1
            found = self.account or TenantAccount(
                TENANT,
                ACCOUNT,
                SELLER,
                TokenPair(SecretStr("stale-access"), SecretStr(TENANT_REFRESH)),
            )
            if isinstance(found, Exception):
                raise found
            return found

        async def resolve(host: str) -> Sequence[str]:
            return ["52.95.150.10"]

        oauth = self.oauth
        return Deps(
            load_account=load_account,
            oauth=lambda settings: oauth,
            transport=httpx.ASGITransport(self.app),
            resolver=resolve,
        )

    def ptd_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.app.state.fake.calls if call.get("kind") == "ptd"]


async def _child_run(child: Child, tmp_path: Path, **over: Any) -> int:
    settings = over.pop("settings", None) or _settings()
    return await ptd_tenant.run(_request(tmp_path, **over), settings=settings, deps=child.deps())


def test_slugs_parse() -> None:
    assert ptd_tenant_slugs("") == frozenset()
    assert ptd_tenant_slugs(" test , Acme ,,") == {"test", "acme"}
    assert Settings(_env_file=None, app_env="local").amazon_ptd_tenant_slugs == ""


# ---- the fetch child (app.workers.ptd_tenant) ----


@pytest.mark.parametrize("slugs", ["", "acme", "acme,other"])
async def test_child_refuses_a_tenant_off_the_allow_list(
    slugs: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    child = Child()
    code = await _child_run(child, tmp_path, settings=_settings(amazon_ptd_tenant_slugs=slugs))
    assert code == ptd_tenant.EXIT_REFUSED
    assert "not on AMAZON_PTD_TENANT_SLUGS" in capsys.readouterr().err
    assert child.loads == 0 and child.oauth is not None and not child.oauth.seen
    assert not (tmp_path / "definition.json").exists()


@pytest.mark.parametrize("missing", ["database_url", "amazon_client_secret", "amazon_app_id"])
async def test_child_needs_its_settings(
    missing: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    child = Child()
    code = await _child_run(child, tmp_path, settings=_settings(**{missing: None}))
    assert code == ptd_tenant.EXIT_FAILED and "is required" in capsys.readouterr().err
    assert child.loads == 0


async def test_child_reports_a_refused_account(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    child = Child(account=Refused("tenant test has no active Amazon account"))
    assert await _child_run(child, tmp_path) == ptd_tenant.EXIT_REFUSED
    assert "refused (tenant test has no active Amazon account)" in capsys.readouterr().err
    assert child.oauth is not None and not child.oauth.seen and not child.ptd_calls()


async def test_child_uses_the_tenant_token_never_the_platform_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_platform(*_: Any, **__: Any) -> Any:
        raise AssertionError("the platform token source must not be built")

    monkeypatch.setattr(definitions, "PlatformTokenSource", no_platform)
    monkeypatch.setattr(cli, "DEFINITIONS_CLIENT", no_platform)
    child = Child()
    assert await _child_run(child, tmp_path) == ptd_tenant.EXIT_OK
    assert child.oauth is not None
    # the one LWA refresh used the tenant's refresh token, and the definitions GET carried the
    # access token it returned (the fake accepts only tokens it issued)
    assert child.oauth.seen == [TENANT_REFRESH]
    assert len(child.ptd_calls()) == 1
    out = tmp_path / "definition.json"
    assert (os.stat(out).st_mode & 0o777) == 0o600
    body = json.loads(out.read_text(encoding="utf-8"))
    assert body["product_type"] == "KURTA" and isinstance(body["schema"], dict)
    assert "digest" not in body
    # never over an existing file
    assert await _child_run(child, tmp_path) == ptd_tenant.EXIT_FAILED


async def test_a_refused_refresh_token_fails_without_a_fetch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app = build_fake()
    child = Child(app, oauth=FakeOAuth(app, refuse=True))
    assert await _child_run(child, tmp_path) == ptd_tenant.EXIT_FAILED
    assert "invalid_grant" in capsys.readouterr().err
    assert not child.ptd_calls()


# ---- the child's account load (tenant_uow, unseal) ----


@dataclass
class _Row:
    id: uuid.UUID
    marketplace: str
    seller_id: str
    status: str


@dataclass
class _Snap:
    marketplace: str
    status: str
    token_enc: bytes | None
    kms_key_version: str | None


class _FakeUow:
    def __init__(self, rows: list[_Row], snap: _Snap | None) -> None:
        self.audits: list[dict[str, Any]] = []

        async def page(**_: Any) -> tuple[list[_Row], None]:
            return rows, None

        async def snapshot(*_: Any, **__: Any) -> _Snap | None:
            return snap

        async def write(**values: Any) -> None:
            self.audits.append(values)

        self.marketplace_accounts = SimpleNamespace(page=page, snapshot=snapshot)
        self.audit = SimpleNamespace(write=write)


def _patch_db(monkeypatch: pytest.MonkeyPatch, uow: _FakeUow) -> list[Any]:
    from app.repositories import uow as uow_module

    unsealed: list[Any] = []

    @asynccontextmanager
    async def tenant_uow(tenant: uuid.UUID, **_: Any) -> AsyncIterator[_FakeUow]:
        yield uow

    async def unseal(*args: Any, **kwargs: Any) -> TokenPair:
        unsealed.append(kwargs)
        return TokenPair(SecretStr("a"), SecretStr(TENANT_REFRESH))

    monkeypatch.setattr(uow_module, "tenant_uow", tenant_uow)
    monkeypatch.setattr(ptd_tenant, "unseal", unseal)
    monkeypatch.setattr(ptd_tenant, "get_key_unwrapper", lambda settings: object())
    return unsealed


ACTIVE = _Row(ACCOUNT, "amazon", SELLER, "active")


@pytest.mark.parametrize(
    ("rows", "snap"),
    [
        ([], None),
        ([_Row(ACCOUNT, "amazon", SELLER, "revoked")], None),
        ([_Row(ACCOUNT, "amazon", SELLER, "needs_reconnect")], None),
        ([_Row(ACCOUNT, "flipkart", SELLER, "active")], None),
        ([ACTIVE], None),
        ([ACTIVE], _Snap("amazon", "active", None, None)),
        ([ACTIVE], _Snap("amazon", "expired", b"x", "v1")),
    ],
)
async def test_load_account_refuses_an_account_that_is_not_active_amazon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rows: list[_Row], snap: _Snap | None
) -> None:
    uow = _FakeUow(rows, snap)
    unsealed = _patch_db(monkeypatch, uow)
    with pytest.raises(Refused) as caught:
        await ptd_tenant.load_account(_settings(), _request(tmp_path))
    assert caught.value.code == ptd_tenant.EXIT_REFUSED
    assert unsealed == [] and uow.audits == []


async def test_load_account_opens_the_active_account_and_audits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    uow = _FakeUow([ACTIVE], _Snap("amazon", "active", b"blob", "v1"))
    unsealed = _patch_db(monkeypatch, uow)
    account = await ptd_tenant.load_account(_settings(), _request(tmp_path))
    assert account.account_id == ACCOUNT and account.seller_id == SELLER
    assert TENANT_REFRESH not in repr(account)
    assert len(unsealed) == 1 and unsealed[0]["tenant_id"] == TENANT
    (audit,) = uow.audits
    assert audit["action"] == "marketplace_account.ptd_fetch"
    assert audit["actor_email"] == "admin@example.com" and audit["entity_id"] == ACCOUNT


# ---- the CLI (app.cli templates refresh --via-tenant) ----


@dataclass
class Cli:
    child: Child
    admin: bool = True
    tenant: uuid.UUID | None = TENANT
    stored: list[tuple[DefinitionRow, Actor]] = field(default_factory=list)
    argvs: list[list[str]] = field(default_factory=list)


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Cli]:
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    state = Cli(Child())

    async def admin(settings: Settings, email: str) -> bool:
        return state.admin

    async def by_slug(slug: str) -> uuid.UUID | None:
        return state.tenant

    def run_child(argv: list[str]) -> int:
        state.argvs.append(argv)
        request = ptd_tenant.request_from(ptd_tenant.build_parser().parse_args(argv[3:]))
        return asyncio.run(
            ptd_tenant.run(request, settings=get_settings(), deps=state.child.deps())
        )

    async def store(engine: Any, row: DefinitionRow, actor: Actor) -> StoreResult:
        state.stored.append((row, actor))
        return StoreResult("stored_current", uuid.uuid4(), row.product_type_version)

    monkeypatch.setattr(cli, "_ptd_admin", admin)
    monkeypatch.setattr(cli, "_tenant_by_slug", by_slug)
    monkeypatch.setattr(cli, "RUN_PTD_CHILD", run_child)
    monkeypatch.setattr(ptd_loads, "store_definition", store)
    yield state
    get_settings.cache_clear()


REFRESH = ["templates", "refresh", "--product-type", "KURTA", "--staff-email", "admin@example.com"]


def _cli(*extra: str) -> int:
    return cli.main([*REFRESH, "--reason", "beta style", "--via-tenant", "test", *extra])


def _result_lines(out: str) -> list[str]:
    return [
        line
        for line in out.splitlines()
        if line.startswith("templates refresh: ") and " version " in line
    ]


@pytest.mark.parametrize("slugs", ["", "acme"])
def test_cli_refuses_a_tenant_off_the_allow_list(
    slugs: str, cli_env: Cli, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AMAZON_PTD_TENANT_SLUGS", slugs)
    get_settings.cache_clear()
    assert _cli() == cli.EXIT_REFUSED
    assert "tenant test is not on AMAZON_PTD_TENANT_SLUGS" in capsys.readouterr().err
    assert cli_env.argvs == [] and cli_env.stored == []


def test_cli_refuses_a_staff_email_that_is_not_a_platform_admin(
    cli_env: Cli, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_env.admin = False
    assert _cli("--dry-run") == cli.EXIT_REFUSED
    assert "not an active platform admin" in capsys.readouterr().err
    assert cli_env.argvs == []


def test_cli_needs_the_migrator_url_even_for_a_dry_run(
    cli_env: Cli, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ALEMBIC_DATABASE_URL")
    get_settings.cache_clear()
    assert _cli("--dry-run") == cli.EXIT_FAILED
    assert "ALEMBIC_DATABASE_URL is required" in capsys.readouterr().err
    assert cli_env.argvs == []


def test_cli_refuses_an_unknown_tenant(cli_env: Cli, capsys: pytest.CaptureFixture[str]) -> None:
    cli_env.tenant = None
    assert _cli() == cli.EXIT_NOT_ELIGIBLE
    assert "no such tenant" in capsys.readouterr().err
    assert cli_env.argvs == []


def test_cli_passes_on_the_child_refusal(cli_env: Cli) -> None:
    cli_env.child.account = Refused("tenant test has no active Amazon account")
    assert _cli() == cli.EXIT_REFUSED
    assert cli_env.stored == []


def test_cli_stores_what_the_child_fetched(
    cli_env: Cli, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli() == cli.EXIT_OK
    (argv,) = cli_env.argvs
    assert argv[1:3] == ["-m", "app.workers.ptd_tenant"]
    assert argv[3:15] == [
        "--tenant-id", str(TENANT), "--tenant-slug", "test", "--product-type", "KURTA",
        "--requirements", "LISTING", "--staff-email", "admin@example.com", "--reason",
        "beta style",
    ]  # fmt: skip
    # the hand-over file lived in a private temporary directory, removed afterwards
    out = Path(argv[argv.index("--out") + 1])
    assert not out.parent.exists()
    row, actor = cli_env.stored[0]
    assert row.product_type == "KURTA" and actor.reason == "beta style (via tenant test)"
    assert row.digest == definitions.canonical_digest(row.schema)
    (line,) = _result_lines(capsys.readouterr().out)
    assert line.startswith("templates refresh: stored_current KURTA version ")
    assert line.endswith("(via tenant test)")


def test_cli_dry_run_saves_the_schema_and_stores_nothing(
    cli_env: Cli, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "kurta.json"
    assert _cli("--dry-run", "--save-schema", str(target)) == cli.EXIT_OK
    assert cli_env.stored == []
    assert target.is_file() and (os.stat(target).st_mode & 0o777) == 0o600
    (line,) = _result_lines(capsys.readouterr().out)
    assert line.startswith("templates refresh: fetched KURTA version ")


def test_cli_refuses_a_result_for_another_product_type(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    out.write_text(json.dumps({"product_type": "SHIRT"}), encoding="utf-8")
    args: Any = SimpleNamespace(product_type="KURTA", requirements="LISTING")
    with pytest.raises(ValueError, match="does not match"):
        cli.read_ptd_result(out, args)


def test_cli_rejects_a_bad_slug() -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main([*REFRESH, "--reason", "r", "--via-tenant", "Not A Slug"])
    assert caught.value.code == 2


def test_no_token_reaches_the_output_or_the_logs(
    cli_env: Cli, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="app")
    assert _cli() == cli.EXIT_OK
    assert _cli("--dry-run") == cli.EXIT_OK
    captured = capsys.readouterr()
    text = captured.out + captured.err + caplog.text
    oauth = cli_env.child.oauth
    assert oauth is not None and len(oauth.issued) == 2
    for secret in (TENANT_REFRESH, PLATFORM_REFRESH, *oauth.issued):
        assert secret not in text
    assert "__ptd_schema" not in text  # nor the pre-signed schema link
    assert SELLER not in text
