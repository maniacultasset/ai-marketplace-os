"""The Amazon connection check: a read-only proof that one tenant's live Amazon account works end
to end. `python -m app.workers.amazon_check --tenant-id T --staff-email E [--ean EAN] [--asin A]`
runs it; the staff CLI's `ops amazon-check --tenant-slug S ...` resolves the slug and hands over
to this entrypoint with `exec` (a worker process: the CLI never imports the decrypt path).

It runs these steps in order, times each one, and returns a PASS / WARN / FAIL / SKIP table:

0. config: the SP-API endpoint matches `AMAZON_USE_SANDBOX`, and the Amazon app is configured.
1. account: the tenant's Amazon account (status, seller id, marketplace, region), its token blob
   opened through the KMS unwrapper, and whether it holds a refresh token. One `audit_log` row
   names the staff actor (`marketplace_account.connection_check`).
2. lwa: a fresh access token from the refresh token, through the same OAuth client the token
   refresh job uses. The new token lives only in memory for this run: the account row is not
   changed (the periodic refresh keeps doing that).
3. participations: Sellers v1 getMarketplaceParticipations; the seller must take part in the
   configured marketplace (Amazon.in, `A21TJRUUN4KGV`).
4. ptd KURTA, ptd SHIRT: getDefinitionsProductType (`LISTING`, `en_IN`) under the seller's token,
   schema downloaded and checksum-verified, and its version compared with the stored current
   definition: in sync, or stale.
5. listings: getListingsItem of a probe SKU that does not exist; 404 proves the Listings role.
6. catalog: searchCatalogItems by one EAN (`--ean`, default a well-known public EAN); the count.
7. restrictions: getListingsRestrictions for `--asin`, else the first ASIN step 6 found.
8. rate limits: the `x-amzn-RateLimit-Limit` header each API answered with.

Read only. Every SP-API request and the schema download go through `GetOnlyTransport`, which
raises `WriteRefused` before anything is sent for any method other than GET: no PUT, PATCH,
DELETE, feed or VALIDATION_PREVIEW can leave this module. The one POST of a run is the LWA token
refresh to `AMAZON_LWA_TOKEN_URL` (step 2), an authorization call that changes no seller data.

Output holds no token, no full payload and no schema: counts, statuses, versions and a masked
seller id only. Errors are reported by class and error code, never by message.

| outcome                              | stream | exit |
|--------------------------------------|--------|------|
| no step failed (PASS or WARN)        | stdout | 0    |
| a step failed                        | stdout | 1    |
| any other failure                    | stderr | 1    |
| bad arguments (argparse)             | stderr | 2    |
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any, Final, Literal

import httpx
from mkos_schemas.codes import ErrorCode
from pydantic import SecretStr

from app.adapters.amazon import constants as c
from app.adapters.amazon.client import ASIN_RE, SpApiClient, failure
from app.adapters.amazon.definitions import (
    DefinitionsClient,
    FetchedDefinition,
    IntervalLimiter,
    LinkPolicy,
)
from app.adapters.amazon.endpoint import spapi_endpoint_errors
from app.adapters.base import AdapterError, OutboundLimiter, PermissionDenied
from app.adapters.credentials import CredentialUnsealError, get_key_unwrapper, unseal
from app.adapters.oauth import OAuthError, OAuthRejectedError, get_oauth_client
from app.core.config import Settings, get_settings
from app.core.crypto import LOCAL_KEY_VERSION, TokenPair
from app.core.logging import configure_logging, get_logger
from app.core.request_context import new_request_id, request_context
from app.repositories.marketplace_accounts import PAGE_LIMIT_MAX
from app.repositories.uow import tenant_uow
from app.workers.token_refresh import REFRESH_MARGIN

log = get_logger(__name__)

Result = Literal["PASS", "WARN", "FAIL", "SKIP"]
DEFAULT_EAN: Final = "5449000000996"  # Coca-Cola 330 ml, the best-known public EAN
PROBE_SKU: Final = "kuinn-connection-check-probe"
PRODUCT_TYPES: Final = ("KURTA", "SHIRT")
ENTITY: Final = "marketplace_account"
AUDIT_ACTION: Final = "marketplace_account.connection_check"
# one call per operation per run; 1/s keeps every bucket well under Amazon's published quota
CHECK_RATES: Final[Mapping[str, int]] = {
    "participations_get": 1,
    "ptd_get": 1,
    "get": 1,
    "catalog_search": 1,
    "restrictions_get": 1,
}
RATE_HEADER: Final = "x-amzn-ratelimit-limit"
_RATE_VALUE: Final = re.compile(r"[0-9]{1,6}(\.[0-9]{1,6})?")
# path prefix -> the API it belongs to (the rate-limit row); the schema download has none
_APIS: Final = (
    ("/sellers/", "sellers"),
    ("/definitions/", "definitions"),
    (f"{c.ITEMS_PATH}/", "listings items"),
    (c.RESTRICTIONS_PATH, "restrictions"),
    ("/catalog/", "catalog"),
)


class WriteRefused(RuntimeError):  # noqa: N818 - names the outcome, as AdapterError's kin do
    """A request other than GET reached the connection check's transport. Nothing was sent."""

    def __init__(self, method: str) -> None:
        super().__init__(f"the Amazon connection check sends GET only, not {method[:16]}")


class GetOnlyTransport(httpx.AsyncBaseTransport):
    """Passes GET requests to `inner` and refuses every other method before it is sent. Counts
    the methods it saw and keeps the rate-limit header per API (never a URL, query or header
    value other than that number)."""

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport(trust_env=False)
        self.methods: Counter[str] = Counter()
        self.refused: Counter[str] = Counter()
        self.rate_limits: dict[str, str] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        if method != "GET":
            self.refused[method] += 1
            raise WriteRefused(method)
        self.methods[method] += 1
        response = await self._inner.handle_async_request(request)
        api = next((name for prefix, name in _APIS if request.url.path.startswith(prefix)), None)
        value = response.headers.get(RATE_HEADER)
        if api is not None and value is not None and _RATE_VALUE.fullmatch(value.strip()):
            self.rate_limits[api] = value.strip()
        return response

    async def aclose(self) -> None:
        """The clients share this transport: `close()` closes the inner one, once."""

    async def close(self) -> None:
        await self._inner.aclose()


class _FreshToken:
    """The access token step 2 just obtained. A 401/403 with it is the answer, not a reason to
    refresh again."""

    def __init__(self, token: SecretStr) -> None:
        self._token = token

    def __repr__(self) -> str:
        return "_FreshToken()"

    async def get(self) -> SecretStr:
        return self._token

    async def refresh(self) -> SecretStr:
        raise PermissionDenied(
            failure(ErrorCode.ACCOUNT_PERMISSION_DENIED, "Amazon refused the fresh access token.")
        )


async def _resolve(host: str) -> Sequence[str]:
    from app.adapters.amazon.definitions import _resolve as resolve

    return await resolve(host)


@dataclass
class CheckDeps:
    """What a run talks through. Production: the real network; tests: the fake marketplace."""

    transport: httpx.AsyncBaseTransport | None = None
    resolver: Callable[[str], Awaitable[Sequence[str]]] = _resolve
    limiter: OutboundLimiter | None = None


def make_check_deps() -> CheckDeps:
    """Tests swap this factory for one over the in-process fake (no network)."""
    return CheckDeps()


@dataclass(frozen=True)
class CheckRow:
    step: str
    result: Result
    detail: str
    ms: float


@dataclass
class CheckReport:
    tenant_id: uuid.UUID
    account_id: uuid.UUID | None = None
    rows: list[CheckRow] = field(default_factory=list)
    gets: int = 0
    refused: int = 0
    token_refreshes: int = 0

    @property
    def result(self) -> Result:
        found = {row.result for row in self.rows}
        if "FAIL" in found or self.refused:
            return "FAIL"
        return "WARN" if "WARN" in found else "PASS"

    @property
    def passed(self) -> bool:
        return self.result != "FAIL"

    def render(self) -> str:
        width = max([len(row.step) for row in self.rows] + [4])
        lines = [
            f"amazon check: tenant {self.tenant_id} account {self.account_id or '-'}",
            f"{'STEP'.ljust(width)}  RESULT  {'TIME'.rjust(8)}  DETAIL",
        ]
        for row in self.rows:
            took = f"{row.ms:.0f} ms".rjust(8)
            lines.append(f"{row.step.ljust(width)}  {row.result.ljust(6)}  {took}  {row.detail}")
        counts = Counter(row.result for row in self.rows)
        lines.append(
            f"amazon check: {self.result} ({counts['PASS']} pass, {counts['WARN']} warn, "
            f"{counts['FAIL']} fail, {counts['SKIP']} skip); requests: {self.gets} GET, "
            f"{self.refused} other refused, {self.token_refreshes} LWA token refresh"
        )
        return "".join(f"{line}\n" for line in lines)


def mask_seller(seller_id: str) -> str:
    """Enough of the seller id to recognise the account: never the whole id."""
    if len(seller_id) <= 6:
        return "*" * len(seller_id)
    return f"{seller_id[:3]}...{seller_id[-3:]}"


def _adapter_detail(exc: AdapterError) -> str:
    code = exc.report.error_code
    return f"{type(exc).__name__} ({code.value if code is not None else '-'})"


@dataclass
class _Account:
    id: uuid.UUID
    seller_id: str
    status: str
    tokens: TokenPair | None


Outcome = tuple[Result, str]


class _Run:
    def __init__(
        self,
        tenant_id: uuid.UUID,
        *,
        staff_email: str,
        ean: str,
        asin: str | None,
        settings: Settings,
        deps: CheckDeps,
        clock: Callable[[], float],
    ) -> None:
        self.tenant_id = tenant_id
        self.staff_email = staff_email
        self.ean = ean
        self.asin = asin
        self.settings = settings
        self.deps = deps
        self.clock = clock
        self.report = CheckReport(tenant_id)
        self.transport = GetOnlyTransport(deps.transport)
        self.account: _Account | None = None
        self.access: SecretStr | None = None
        self.sp: SpApiClient | None = None
        self.definitions: DefinitionsClient | None = None

    def skip(self, step: str, why: str) -> None:
        self.report.rows.append(CheckRow(step, "SKIP", why, 0.0))

    async def step(self, name: str, body: Callable[[], Awaitable[Outcome]]) -> Result:
        started = self.clock()
        try:
            result, detail = await body()
        except WriteRefused:
            result, detail = "FAIL", "refused a request that is not a GET (nothing was sent)"
        except AdapterError as exc:
            result, detail = "FAIL", _adapter_detail(exc)
        except Exception as exc:  # the class only: a message can carry a URL or a value
            result, detail = "FAIL", f"failed ({type(exc).__name__})"
        ms = (self.clock() - started) * 1000
        self.report.rows.append(CheckRow(name, result, detail, round(ms, 1)))
        return result

    # ---- 0. config ----

    async def config(self) -> Outcome:
        s = self.settings
        problems = spapi_endpoint_errors(
            s.amazon_spapi_endpoint, use_sandbox=s.amazon_use_sandbox, app_env=s.app_env
        )
        if problems:
            return "FAIL", "; ".join(problems)
        if get_oauth_client("amazon", s) is None:
            return "FAIL", "the Amazon app is not configured (AMAZON_APP_ID, client id, secret)"
        mode = "sandbox" if s.amazon_use_sandbox else "production"
        return "PASS", f"SP-API {mode} endpoint, marketplace {s.amazon_marketplace_id}"

    # ---- 1. account ----

    async def load_account(self) -> Outcome:
        async with tenant_uow(self.tenant_id) as uow:
            rows, _ = await uow.marketplace_accounts.page(limit=PAGE_LIMIT_MAX, after=None)
            found = [r for r in rows if r.marketplace == "amazon" and r.status != "revoked"]
            if not found:
                return "FAIL", "no Amazon account is connected for this tenant"
            row = found[0]
            account_id, seller_id, region, status = row.id, row.seller_id, row.region, row.status
            snap = await uow.marketplace_accounts.snapshot(account_id, margin=REFRESH_MARGIN)
            await uow.audit.write(
                tenant_id=self.tenant_id,
                actor_type="staff",
                actor_user_id=None,
                actor_email=self.staff_email,
                action=AUDIT_ACTION,
                entity_type=ENTITY,
                entity_id=account_id,
                reason="amazon_connection_check",
            )
        self.report.account_id = account_id
        if snap is None or snap.token_enc is None or snap.kms_key_version is None:
            self.account = _Account(account_id, seller_id, status, None)
            return "FAIL", f"status {status}: no stored token (reconnect the account)"
        try:
            tokens = await unseal(
                snap.token_enc,
                key_version=snap.kms_key_version,
                tenant_id=self.tenant_id,
                unwrapper=get_key_unwrapper(self.settings),
            )
        except CredentialUnsealError:
            self.account = _Account(account_id, seller_id, snap.status, None)
            return "FAIL", "the stored token did not open (KMS unwrap or the blob failed)"
        self.account = _Account(account_id, seller_id, snap.status, tokens)
        sealed = "local key" if snap.kms_key_version == LOCAL_KEY_VERSION else "Cloud KMS"
        refresh = "present" if tokens.refresh_token.get_secret_value() else "MISSING"
        expires = "unknown"
        if snap.expires_at is not None:
            minutes = int((snap.expires_at - datetime.now(UTC)).total_seconds() // 60)
            expires = f"{minutes} min" if minutes >= 0 else f"expired {-minutes} min ago"
        detail = (
            f"status {snap.status}, seller {mask_seller(seller_id)}, marketplace "
            f"{self.settings.amazon_marketplace_id}, region {region or '-'}, token sealed "
            f"({sealed}) and opened, refresh token {refresh}, stored access token {expires}"
        )
        if snap.status != "active" or refresh != "present":
            return "FAIL", detail
        return "PASS", detail

    # ---- 2. LWA ----

    async def lwa(self) -> Outcome:
        client = get_oauth_client("amazon", self.settings)
        if self.account is None or self.account.tokens is None:
            return "FAIL", "no usable account token"
        if client is None:
            return "FAIL", "the Amazon app is not configured"
        self.report.token_refreshes += 1
        try:
            grant = await client.refresh(self.account.tokens.refresh_token)
        except OAuthRejectedError:
            return "FAIL", "Amazon refused the refresh token (invalid_grant): reconnect"
        except OAuthError as exc:
            return "FAIL", f"the token refresh did not complete ({type(exc).__name__})"
        self.access = grant.tokens.access_token
        lifetime = "unknown"
        if grant.expires_at is not None:
            lifetime = f"{int((grant.expires_at - datetime.now(UTC)).total_seconds() // 60)} min"
        return "PASS", f"fresh access token issued (valid {lifetime}; kept in memory only)"

    def clients(self) -> tuple[SpApiClient, DefinitionsClient]:
        if self.sp is None or self.definitions is None:
            if self.account is None or self.access is None:
                raise RuntimeError("the SP-API steps need the account and a fresh token")
            limiter = self.deps.limiter or IntervalLimiter(CHECK_RATES)
            self.sp = SpApiClient(
                endpoint=self.settings.amazon_spapi_endpoint,
                marketplace_id=self.settings.amazon_marketplace_id,
                seller_id=self.account.seller_id,
                tokens=_FreshToken(self.access),
                limiter=limiter,
                transport=self.transport,
            )
            self.definitions = DefinitionsClient(
                self.sp,
                marketplace_id=self.settings.amazon_marketplace_id,
                link_policy=LinkPolicy.from_setting(self.settings.amazon_ptd_link_hosts),
                transport=self.transport,
                resolver=self.deps.resolver,
            )
        return self.sp, self.definitions

    # ---- 3. participations ----

    async def participations(self) -> Outcome:
        sp, _ = self.clients()
        reply = await sp.get_participations()
        if reply.status != 200 or not isinstance(reply.json, dict):
            return "FAIL", f"Amazon answered {reply.status}"
        payload = reply.json.get("payload")
        entries = [e for e in payload if isinstance(e, dict)] if isinstance(payload, list) else []
        wanted = self.settings.amazon_marketplace_id
        mine = next((e for e in entries if _marketplace_id(e) == wanted), None)
        if mine is None:
            return "FAIL", f"the seller does not take part in {wanted} ({len(entries)} other)"
        part = mine.get("participation")
        part = part if isinstance(part, dict) else {}
        name = _marketplace_name(mine) or wanted
        if part.get("isParticipating") is not True:
            return "FAIL", f"{name} ({wanted}): not participating"
        if part.get("hasSuspendedListings") is True:
            return "WARN", f"{name} ({wanted}): participating, has suspended listings"
        return "PASS", f"{name} ({wanted}): participating, active ({len(entries)} marketplaces)"

    # ---- 4. product type definitions ----

    async def ptd(self, product_type: str) -> Outcome:
        _, definitions = self.clients()
        fetched = await definitions.get_product_type(product_type)
        async with tenant_uow(self.tenant_id) as uow:
            stored = await uow.ptd.current(
                marketplace_id=self.settings.amazon_marketplace_id,
                product_type=product_type,
                requirements=c.PTD_REQUIREMENTS,
                locale=c.PTD_LOCALE,
            )
        props = _count(fetched.schema.get("properties"))
        head = f"schema returned, version {fetched.product_type_version}, {props} properties"
        if stored is None:
            return "WARN", f"{head}; no stored definition (run templates refresh)"
        if _same(fetched, stored.product_type_version, stored.digest):
            return "PASS", f"{head}; in sync with the stored definition"
        return "WARN", (
            f"{head}; stored version {stored.product_type_version} is stale "
            f"(run templates refresh, then promote-definition)"
        )

    # ---- 5. listings items ----

    async def listings(self) -> Outcome:
        sp, _ = self.clients()
        reply = await sp.get(PROBE_SKU)
        if reply.status == 404:
            return "PASS", "404 for a probe SKU that does not exist (Listings read allowed)"
        if reply.status == 200:
            return "WARN", "the probe SKU exists on this account (expected 404)"
        return "FAIL", f"Amazon answered {reply.status} (expected 404)"

    # ---- 6. catalog ----

    async def catalog(self) -> Outcome:
        sp, _ = self.clients()
        reply = await sp.search_catalog([self.ean], "EAN")
        if reply.status != 200 or not isinstance(reply.json, dict):
            return "FAIL", f"Amazon answered {reply.status}"
        items = reply.json.get("items")
        found = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
        asins = [i["asin"] for i in found if isinstance(i.get("asin"), str)]
        if self.asin is None:
            self.asin = next((a for a in asins if ASIN_RE.fullmatch(a)), None)
        partial = " (partial: more pages)" if "pagination" in reply.json else ""
        return "PASS", f"{len(found)} item(s) for EAN {self.ean}{partial}"

    # ---- 7. restrictions ----

    async def restrictions(self) -> Outcome:
        if self.asin is None:
            return "SKIP", "no ASIN"
        sp, _ = self.clients()
        reply = await sp.get_restrictions(self.asin)
        if reply.status != 200 or not isinstance(reply.json, dict):
            return "FAIL", f"Amazon answered {reply.status}"
        rows = reply.json.get("restrictions")
        found = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        codes = sorted({code for r in found for code in _reason_codes(r)})
        why = f" ({', '.join(codes)})" if codes else ""
        return "PASS", f"{len(found)} restriction(s) for ASIN {self.asin}{why}"

    # ---- 8. rate limits ----

    async def rate_limits(self) -> Outcome:
        seen = self.transport.rate_limits
        if not seen:
            return "WARN", "no x-amzn-RateLimit-Limit header seen"
        return "PASS", ", ".join(f"{api} {value}/s" for api, value in sorted(seen.items()))

    async def run(self) -> CheckReport:
        try:
            await self._steps()
        finally:
            if self.definitions is not None:
                await self.definitions.aclose()  # closes the SP-API client too
            await self.transport.close()
            self.access = None
            self.account = None
        self.report.gets = self.transport.methods["GET"]
        self.report.refused = sum(self.transport.refused.values())
        log.info(
            "amazon_check.done",
            tenant_id=str(self.tenant_id),
            account_id=str(self.report.account_id) if self.report.account_id else None,
            result=self.report.result,
            steps={row.step: row.result for row in self.report.rows},
            gets=self.report.gets,
            refused=self.report.refused,
        )
        return self.report

    async def _steps(self) -> None:
        names = ["1 account", "2 lwa", "3 participations"]
        names += [f"4 ptd {t}" for t in PRODUCT_TYPES]
        names += ["5 listings", "6 catalog", "7 restrictions", "8 rate limits"]
        if await self.step("0 config", self.config) == "FAIL":
            for name in names:
                self.skip(name, "the configuration check failed")
            return
        await self.step(names[0], self.load_account)
        if self.account is None or self.account.tokens is None:
            for name in names[1:]:
                self.skip(name, "no usable account token")
            return
        if await self.step(names[1], self.lwa) == "FAIL" or self.access is None:
            for name in names[2:]:
                self.skip(name, "no fresh access token")
            return
        await self.step(names[2], self.participations)
        for product_type in PRODUCT_TYPES:
            await self.step(f"4 ptd {product_type}", partial(self.ptd, product_type))
        await self.step("5 listings", self.listings)
        await self.step("6 catalog", self.catalog)
        if self.asin is None:
            self.skip("7 restrictions", "no ASIN: the EAN matched nothing (pass --asin)")
        else:
            await self.step("7 restrictions", self.restrictions)
        await self.step("8 rate limits", self.rate_limits)


def _marketplace_id(entry: Mapping[str, Any]) -> str | None:
    market = entry.get("marketplace")
    value = market.get("id") if isinstance(market, dict) else None
    return value if isinstance(value, str) else None


def _marketplace_name(entry: Mapping[str, Any]) -> str | None:
    market = entry.get("marketplace")
    value = market.get("name") if isinstance(market, dict) else None
    return value[:40] if isinstance(value, str) else None


def _reason_codes(restriction: Mapping[str, Any]) -> list[str]:
    reasons = restriction.get("reasons")
    if not isinstance(reasons, list):
        return []
    return [
        str(reason["reasonCode"])[:40]
        for reason in reasons
        if isinstance(reason, dict) and reason.get("reasonCode")
    ]


def _count(value: Any) -> int:
    return len(value) if isinstance(value, dict | list) else 0


def _same(fetched: FetchedDefinition, version: str, digest: bytes) -> bool:
    return fetched.product_type_version == version and fetched.digest == digest


async def run_check(
    tenant_id: uuid.UUID,
    *,
    staff_email: str,
    ean: str = DEFAULT_EAN,
    asin: str | None = None,
    deps: CheckDeps | None = None,
    settings: Settings | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CheckReport:
    """Run every step for `tenant_id`'s Amazon account; never raises for a failed step."""
    run = _Run(
        tenant_id,
        staff_email=staff_email,
        ean=ean,
        asin=asin,
        settings=settings or get_settings(),
        deps=deps or make_check_deps(),
        clock=clock,
    )
    return await run.run()


# ---- the entrypoint ----

EAN_RE: Final = re.compile(r"[0-9]{8,14}", re.ASCII)
EMAIL_RE: Final = re.compile(r"[^@\s]{1,64}@[^@\s]{1,189}")


def _tenant_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--tenant-id must be a tenant UUID") from None


def _email(value: str) -> str:
    if not EMAIL_RE.fullmatch(value.strip()):
        raise argparse.ArgumentTypeError("--staff-email must be an email address")
    return value.strip().lower()


def _ean(value: str) -> str:
    if not EAN_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("--ean must be 8 to 14 digits")
    return value


def _asin(value: str) -> str:
    if not ASIN_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("--asin must be 10 of [A-Z0-9]")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.workers.amazon_check")
    parser.add_argument("--tenant-id", type=_tenant_id, required=True)
    parser.add_argument("--staff-email", type=_email, required=True)
    parser.add_argument("--ean", type=_ean, default=DEFAULT_EAN)
    parser.add_argument("--asin", type=_asin, default=None)
    return parser


async def _main(args: argparse.Namespace) -> int:
    from app.db.session import dispose_engine

    try:
        with request_context(new_request_id()):
            report = await run_check(
                args.tenant_id, staff_email=args.staff_email, ean=args.ean, asin=args.asin
            )
    except Exception as exc:  # the class only, never the message
        sys.stderr.write(f"amazon check: failed ({type(exc).__name__})\n")
        return 1
    finally:
        await dispose_engine()
    sys.stdout.write(report.render())
    return 0 if report.passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(get_settings())
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
