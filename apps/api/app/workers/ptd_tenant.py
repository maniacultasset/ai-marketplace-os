"""Amazon Product Type Definitions through an allow-listed seller connection (ADR-0033): the fetch.

`python -m app.cli templates refresh --product-type T --staff-email E --reason R --via-tenant S
[--dry-run [--save-schema PATH]]` checks the allow-list and the platform admin, resolves S, and
runs this entrypoint as a child process (the CLI never imports the decrypt path; this module never
writes SQL, R4):

    python -m app.workers.ptd_tenant --tenant-id ID --tenant-slug S --product-type T
        --requirements R --staff-email E --reason R --out PATH

It fetches the definition the way `templates refresh` does under the platform authorization
(`DefinitionsClient`: checksum, link policy and body cap included), with one difference: the
access token comes from tenant S's stored Amazon connection, refreshed in memory as
`app.workers.amazon_check` does (the account row is not changed).

Guards, in order, before any token is opened: S is on `AMAZON_PTD_TENANT_SLUGS` again (this
entrypoint can be run by hand), the Amazon app and `DATABASE_URL` are set, the SP-API endpoint
matches `AMAZON_USE_SANDBOX`, ID is tenant S, in trial or active (its own row, read under RLS), and that
tenant has exactly one Amazon account, `active` with a stored token. One
audit row on the account names the staff email and the product type
(`marketplace_account.ptd_fetch`). Then the token blob is opened (KMS), one LWA refresh gives an
access token kept in memory only, and `getDefinitionsProductType` plus the pre-signed schema
download go through `GetOnlyTransport`: nothing other than a GET can leave.

The fetched definition (its fields and the verified schema, never a token or the schema link) is
written to `--out`, a new 0600 file the CLI reads, checks and stores. Errors are reported by class
and error code, never by message.

| outcome                              | stream | exit |
|--------------------------------------|--------|------|
| fetched (written to `--out`)         | -      | 0    |
| a missing setting, any failure       | stderr | 1    |
| bad arguments (argparse)             | stderr | 2    |
| refused (allow-list, ID is not S, no single active Amazon account with a token) | stderr | 3 |
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx

from app.adapters.amazon import constants as c
from app.adapters.amazon.client import SpApiClient
from app.adapters.amazon.definitions import (
    DefinitionsClient,
    FetchedDefinition,
    IntervalLimiter,
    LinkPolicy,
)
from app.adapters.amazon.endpoint import spapi_endpoint_errors
from app.adapters.credentials import CredentialUnsealError, get_key_unwrapper, unseal
from app.adapters.oauth import OAuthError, OAuthRejectedError, get_oauth_client
from app.core.config import Settings, get_settings, ptd_tenant_slugs
from app.core.crypto import TokenPair
from app.core.logging import configure_logging, get_logger
from app.core.redaction import mask_seller
from app.core.request_context import new_request_id, request_context
from app.workers.amazon_check import FreshToken, GetOnlyTransport, _resolve

log = get_logger(__name__)

LABEL: Final = "templates refresh"
EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_REFUSED: Final = 3
ENTITY: Final = "marketplace_account"
AUDIT_ACTION: Final = "marketplace_account.ptd_fetch"
# one definitions GET per run; the schema download is not an SP-API operation
RATES: Final[Mapping[str, int]] = {"ptd_get": 1}
# a tenant that may still use its connection (`suspended` and `offboarded` may not)
LIVE_TENANT: Final = frozenset({"trial", "active"})
SLUG_RE: Final = re.compile(r"[a-z0-9][a-z0-9-]{1,39}", re.ASCII)  # the tenant slug rule
PRODUCT_TYPE_RE: Final = re.compile(r"[A-Z0-9_]{1,64}", re.ASCII)
EMAIL_RE: Final = re.compile(r"[^@\s]{1,64}@[^@\s]{1,189}")
# the fields of the hand-over file, in `FetchedDefinition` order (the CLI recomputes the digest)
FIELDS: Final = (
    "marketplace_id",
    "product_type",
    "requirements",
    "locale",
    "product_type_version",
    "requirements_enforced",
    "display_name",
    "property_groups",
    "meta_schema_id",
    "schema",
)


class Refused(Exception):  # noqa: N818 - names the outcome
    """A guard refused the run. The message names settings and states, never a value."""

    def __init__(self, message: str, *, code: int = EXIT_REFUSED) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TenantAccount:
    """The opened connection of the allow-listed tenant (the tokens never print)."""

    tenant_id: uuid.UUID
    account_id: uuid.UUID
    seller_id: str
    tokens: TokenPair = field(repr=False)


@dataclass(frozen=True)
class Request:
    tenant_id: uuid.UUID
    slug: str
    product_type: str
    requirements: str
    staff_email: str
    reason: str
    out: Path


# ---- the production seams (tests swap `make_deps`) ----


async def load_account(settings: Settings, request: Request) -> TenantAccount:
    """The tenant's active Amazon account, its token blob opened through the KMS unwrapper; one
    audit row names the staff email. `Refused` when there is no such account."""
    from app.repositories.marketplace_accounts import PAGE_LIMIT_MAX
    from app.repositories.uow import tenant_uow
    from app.workers.token_refresh import REFRESH_MARGIN

    async with tenant_uow(request.tenant_id) as uow:
        # the allow-list was checked on the slug: the id must name that same tenant (its own row,
        # under RLS), or a hand run could pair an allowed slug with another brand's id
        profile = await uow.tenants.get_profile()
        if profile is None or profile.slug != request.slug:
            raise Refused(f"--tenant-id is not the tenant {request.slug}")
        if profile.status not in LIVE_TENANT or profile.offboarded_at is not None:
            raise Refused(f"tenant {request.slug} is suspended or offboarded")
        rows, _ = await uow.marketplace_accounts.page(limit=PAGE_LIMIT_MAX, after=None)
        amazon = [r for r in rows if r.marketplace == "amazon" and r.status == "active"]
        if not amazon:
            raise Refused(f"tenant {request.slug} has no active Amazon account")
        if len(amazon) > 1:
            raise Refused(f"tenant {request.slug} has more than one active Amazon account")
        row = amazon[0]
        snap = await uow.marketplace_accounts.snapshot(row.id, margin=REFRESH_MARGIN)
        if (
            snap is None
            or snap.marketplace != "amazon"
            or snap.status != "active"
            or snap.token_enc is None
            or snap.kms_key_version is None
        ):
            raise Refused(f"tenant {request.slug} has no active Amazon account with a stored token")
        await uow.audit.write(
            tenant_id=request.tenant_id,
            actor_type="staff",
            actor_user_id=None,
            actor_email=request.staff_email,
            action=AUDIT_ACTION,
            entity_type=ENTITY,
            entity_id=row.id,
            reason=f"ptd {request.product_type}: {request.reason}"[:1000],
        )
        token_enc, key_version = snap.token_enc, snap.kms_key_version
    try:
        tokens = await unseal(
            token_enc,
            key_version=key_version,
            tenant_id=request.tenant_id,
            unwrapper=get_key_unwrapper(settings),
        )
    except CredentialUnsealError:
        raise Refused("the stored Amazon token did not open", code=EXIT_FAILED) from None
    if not tokens.refresh_token.get_secret_value():
        raise Refused(f"tenant {request.slug} has no Amazon refresh token (reconnect)")
    return TenantAccount(request.tenant_id, row.id, row.seller_id, tokens)


@dataclass
class Deps:
    """What a run talks through. Production: the database, KMS and the network; tests: fakes."""

    load_account: Callable[[Settings, Request], Awaitable[TenantAccount]] = load_account
    oauth: Callable[[Settings], Any] = lambda settings: get_oauth_client("amazon", settings)
    transport: httpx.AsyncBaseTransport | None = None
    resolver: Callable[[str], Awaitable[Sequence[str]]] = _resolve


def make_deps() -> Deps:
    """Tests swap this factory for one over the in-process fake (no network, no database)."""
    return Deps()


# ---- the run ----


def missing_setting(settings: Settings) -> str | None:
    if not settings.amazon_app_id or not settings.amazon_client_id:
        return "AMAZON_APP_ID and AMAZON_CLIENT_ID"
    if not settings.amazon_client_secret:
        return "AMAZON_CLIENT_SECRET"
    if settings.database_url is None:
        return "DATABASE_URL"
    return None


async def _access_token(settings: Settings, account: TenantAccount, deps: Deps) -> FreshToken:
    client = deps.oauth(settings)
    if client is None:
        raise Refused("the Amazon app is not configured", code=EXIT_FAILED)
    try:
        grant = await client.refresh(account.tokens.refresh_token)
    except OAuthRejectedError:
        raise Refused(
            "Amazon refused the tenant's refresh token (invalid_grant): reconnect",
            code=EXIT_FAILED,
        ) from None
    except OAuthError as exc:
        raise Refused(
            f"the token refresh did not complete ({type(exc).__name__})", code=EXIT_FAILED
        ) from None
    return FreshToken(grant.tokens.access_token)


async def fetch(
    settings: Settings, request: Request, account: TenantAccount, deps: Deps
) -> FetchedDefinition:
    """`getDefinitionsProductType` and the schema download under the tenant's fresh access
    token, through a GET-only transport."""
    tokens = await _access_token(settings, account, deps)
    transport = GetOnlyTransport(deps.transport)
    sp = SpApiClient(
        endpoint=settings.amazon_spapi_endpoint,
        marketplace_id=settings.amazon_marketplace_id,
        seller_id=account.seller_id,
        tokens=tokens,
        limiter=IntervalLimiter(RATES),
        transport=transport,
    )
    client = DefinitionsClient(
        sp,
        marketplace_id=settings.amazon_marketplace_id,
        link_policy=LinkPolicy.from_setting(settings.amazon_ptd_link_hosts),
        transport=transport,
        resolver=deps.resolver,
    )
    try:
        return await client.get_product_type(
            request.product_type, requirements=request.requirements
        )
    finally:
        await client.aclose()  # closes the SP-API client too
        await transport.close()


def write_result(path: Path, fetched: FetchedDefinition) -> None:
    """The hand-over file: a new file, mode 0600, never over an existing file or a link."""
    body = {name: getattr(fetched, name) for name in FIELDS}
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(body, handle, ensure_ascii=False, sort_keys=True)


def _failure(exc: BaseException) -> str:
    report = getattr(exc, "report", None)
    code = f", {report.error_code}" if report is not None and report.error_code else ""
    return f"{type(exc).__name__}{code}"


async def run(request: Request, *, settings: Settings, deps: Deps) -> int:
    """Every guard, then the fetch; the exit code (a stderr line unless it is 0)."""
    if request.slug not in ptd_tenant_slugs(settings.amazon_ptd_tenant_slugs):
        sys.stderr.write(
            f"{LABEL}: refused (tenant {request.slug} is not on AMAZON_PTD_TENANT_SLUGS)\n"
        )
        return EXIT_REFUSED
    missing = missing_setting(settings)
    if missing is not None:
        sys.stderr.write(f"{LABEL}: failed ({missing} is required)\n")
        return EXIT_FAILED
    if spapi_endpoint_errors(
        settings.amazon_spapi_endpoint,
        use_sandbox=settings.amazon_use_sandbox,
        app_env=settings.app_env,
    ):
        sys.stderr.write(
            f"{LABEL}: failed (AMAZON_SPAPI_ENDPOINT does not match AMAZON_USE_SANDBOX)\n"
        )
        return EXIT_FAILED
    try:
        account = await deps.load_account(settings, request)
        fetched = await fetch(settings, request, account, deps)
    except Refused as exc:
        outcome = "failed" if exc.code == EXIT_FAILED else "refused"
        sys.stderr.write(f"{LABEL}: {outcome} ({exc})\n")
        return exc.code
    except Exception as exc:  # class and code only, never the message (it may name the link)
        sys.stderr.write(f"{LABEL}: failed ({_failure(exc)})\n")
        return EXIT_FAILED
    try:
        write_result(request.out, fetched)
    except OSError as exc:
        sys.stderr.write(f"{LABEL}: failed (the result was not written: {type(exc).__name__})\n")
        return EXIT_FAILED
    log.info(
        "ptd.via_tenant.fetched",
        tenant_id=str(account.tenant_id),
        account_id=str(account.account_id),
        seller=mask_seller(account.seller_id),
        product_type=fetched.product_type,
        version=fetched.product_type_version,
    )
    return EXIT_OK


# ---- the entrypoint ----


def _tenant_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--tenant-id must be a tenant UUID") from None


def _slug(value: str) -> str:
    if not SLUG_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("--tenant-slug must be a tenant slug")
    return value


def _product_type(value: str) -> str:
    if not PRODUCT_TYPE_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("--product-type must match [A-Z0-9_]{1,64}")
    return value


def _email(value: str) -> str:
    if not EMAIL_RE.fullmatch(value.strip()):
        raise argparse.ArgumentTypeError("--staff-email must be an email address")
    return value.strip().lower()


def _reason(value: str) -> str:
    text = value.strip()
    if not text or len(text) > 500:
        raise argparse.ArgumentTypeError("--reason must be 1-500 characters")
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.workers.ptd_tenant")
    parser.add_argument("--tenant-id", type=_tenant_id, required=True)
    parser.add_argument("--tenant-slug", type=_slug, required=True)
    parser.add_argument("--product-type", type=_product_type, required=True)
    parser.add_argument(
        "--requirements", choices=c.PTD_REQUIREMENTS_VALUES, default=c.PTD_REQUIREMENTS
    )
    parser.add_argument("--staff-email", type=_email, required=True)
    parser.add_argument("--reason", type=_reason, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def request_from(args: argparse.Namespace) -> Request:
    return Request(
        tenant_id=args.tenant_id,
        slug=args.tenant_slug,
        product_type=args.product_type,
        requirements=args.requirements,
        staff_email=args.staff_email,
        reason=args.reason,
        out=args.out,
    )


async def _main(request: Request) -> int:
    from app.db.session import dispose_engine

    try:
        with request_context(new_request_id()):
            return await run(request, settings=get_settings(), deps=make_deps())
    finally:
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(get_settings())
    return asyncio.run(_main(request_from(args)))


if __name__ == "__main__":
    raise SystemExit(main())
