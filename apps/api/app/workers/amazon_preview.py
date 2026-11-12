"""The owner's one-off Amazon dry run (decision of 2026-11-12; docs/runbooks/AMAZON-PREVIEW-ONEOFF.md;
ADR-0028 amendment). `python -m app.workers.amazon_preview --tenant-id T --listing-id L
--staff-email E [--precheck]` runs Amazon's `VALIDATION_PREVIEW` (which saves nothing) for one
listing, now, in this process, and prints what Amazon said. The staff CLI's `ops amazon-preview
--tenant-slug S ...` resolves the tenant and hands over to this entrypoint with `exec` (a worker
process: the CLI never imports the decrypt path or the SP-API client).

Guards, in order; any refusal sends nothing to Amazon:

1. Settings (exit 3): `AMAZON_PREVIEW_ONEOFF` (only `deploy.sh amazon-preview` sets it, and only
   the shell role may boot with it), `AMAZON_VALIDATION_PREVIEW_ENABLED`, `AMAZON_PUBLISH_ENABLED`,
   and `AMAZON_CATALOG_PRECHECK_ENABLED` exactly when `--precheck` is passed.
2. The staff email is an active platform admin (exit 4).
3. The listing (exit 4): Amazon, not paused or archived, `validated`, `needs_review`, `approved`
   or `blocked/validation_errors`, with a current version; its account `active`; the account's
   preview breaker (CP-D24) closed; no live `listing_status_sync` job of the listing; and every
   seller SKU of the product and of every variant is a test SKU (`KTEST-` prefix, the owner's
   rule: never a live product); the built item's SKUs are checked again right before the preview. One `listing.preview_oneoff` audit row names the staff actor.
4. A GET (getListingsItem) of each of the listing's SKUs. Any SKU found on Amazon: STOP (exit 5),
   and no preview is sent.

Then the preview itself is `app.workers.remote_preview.run_remote_preview`, the job body as it
stands (the same item build, the same `PreviewView`, the same pre-check join), called with a job
context of its own. Its `persist` (the issue rows and the status move) is NOT run: with the dry
run off in production, a verdict written here could leave a listing blocked with no later preview
to clear it. The resolution loop and its resend never run (no terminal hook is called), and the
whole run sits inside `no_enqueue`, so no job of any kind can be queued from this process.

Every SP-API request goes through `PreviewOnlyTransport`: a GET, or a PUT on a listings item whose
only `mode` is `VALIDATION_PREVIEW`; anything else raises `PreviewWriteRefused` before it is sent.
The adapter reaches the preview view through `_PreviewOnly`, which has no create, update or
publish method at all.

After the preview, a GET of each SKU again. The output is the verdict, each issue (code, Amazon's
code, severity, attribute path, message), each SKU's reply status and its GET result. A SKU found
on Amazon, a reply that looked applied, or a request the transport refused prints STOP and exits
5, and any such STOP after the preview also opens the account's breaker task (CP-D24, our own
row), so no later dry run of the account runs until staff close it.

| outcome                                  | stream | exit |
|------------------------------------------|--------|------|
| verdict printed, every SKU 404           | stdout | 0    |
| any failure (Amazon unreachable, a bug)  | stderr | 1    |
| bad arguments (argparse)                 | stderr | 2    |
| refused by the settings                  | stderr | 3    |
| not eligible (staff, listing, account)   | stderr | 4    |
| STOP: found on Amazon, or looked applied | stdout | 5    |
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import httpx
from mkos_schemas.enums import JobKind

from app.adapters.amazon import constants as c
from app.adapters.base import (
    AdapterListing,
    NullSink,
    PrecheckResult,
    RemotePreview,
)
from app.core.config import Settings, get_settings
from app.core.flags import publish_enabled
from app.core.logging import configure_logging, get_logger
from app.core.request_context import new_request_id, request_context
from app.repositories.jobs import EnqueueRefused, no_enqueue
from app.repositories.uow import tenant_uow
from app.services.adapter_input import seller_skus
from app.services.remote_preview import open_breaker, previewable
from app.workers import previewers
from app.workers.errors import JobFailed, RetryableJobError
from app.workers.payloads import StatusSyncPayload, dump
from app.workers.readers import AccountInactive, ReaderView, ReadFailed
from app.workers.registry import JobContext
from app.workers.remote_preview import run_remote_preview, with_precheck

if TYPE_CHECKING:
    from app.repositories.listings import PreviewSource

log = get_logger(__name__)

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_REFUSED: Final = 3
EXIT_NOT_ELIGIBLE: Final = 4
EXIT_STOP: Final = 5
REASON: Final = "amazon_preview_oneoff"
AUDIT_ACTION: Final = "listing.preview_oneoff"
PREVIEW_MODE: Final = "VALIDATION_PREVIEW"
VERDICTS: Final = frozenset({"VALID", "INVALID"})
# the owner allowed the dry run on test listings only (2026-11-12): every SKU must carry it
TEST_SKU_PREFIX: Final = "KTEST-"


class Refused(Exception):  # noqa: N818 - an outcome, as the CLI's NotEligibleError
    """The settings do not allow the one-off run (exit 3). Nothing was sent."""


class NotEligible(Exception):  # noqa: N818
    """The staff email, the listing or its account may not be previewed (exit 4)."""


class PreviewWriteRefused(RuntimeError):  # noqa: N818
    """A request other than a GET or a VALIDATION_PREVIEW PUT reached the transport. Not sent."""

    def __init__(self, method: str) -> None:
        super().__init__(
            f"the one-off dry run sends GET and VALIDATION_PREVIEW only, not {method[:16]}"
        )


def _is_preview_put(request: httpx.Request) -> bool:
    """A PUT on `/listings/<version>/items/<seller>/<sku>` with exactly one `mode`, the preview."""
    path = request.url.path
    prefix = f"{c.ITEMS_PATH}/"
    if not path.startswith(prefix):
        return False
    rest = request.url.raw_path.split(b"?", 1)[0][len(prefix) :]
    return rest.count(b"/") == 1 and request.url.params.get_list("mode") == [PREVIEW_MODE]


@dataclass
class TransportLog:
    """What the run's transports saw: one (method, kind) per request, never a URL or SKU."""

    calls: list[tuple[str, str]] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)

    def summary(self) -> str:
        gets = sum(1 for method, _ in self.calls if method == "GET")
        previews = sum(1 for _, kind in self.calls if kind == "preview")
        return f"{gets} GET, {previews} PUT (VALIDATION_PREVIEW), {len(self.refused)} refused"


class PreviewOnlyTransport(httpx.AsyncBaseTransport):
    """Passes a GET or a VALIDATION_PREVIEW PUT to `inner`; refuses anything else unsent."""

    def __init__(self, inner: httpx.AsyncBaseTransport | None, seen: TransportLog) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport(trust_env=False)
        self._seen = seen

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        if method == "GET":
            kind = "read"
        elif method == "PUT" and _is_preview_put(request):
            kind = "preview"
        else:
            self._seen.refused.append(method[:16])
            log.error("amazon_preview.write_refused", method=method[:16])
            raise PreviewWriteRefused(method)
        self._seen.calls.append((method, kind))
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class _PreviewOnly:
    """The adapter as the preview view sees it in this run: the dry run and the read-only
    pre-check, nothing else (no create, update or publish attribute exists). It keeps the last
    verdict and pre-check so the run can print them."""

    __slots__ = ("__adapter", "precheck", "verdict")

    def __init__(self, adapter: object) -> None:
        self.__adapter = adapter
        self.verdict: RemotePreview | None = None
        self.precheck: PrecheckResult | None = None

    def __repr__(self) -> str:
        return "_PreviewOnly()"

    async def preview_remote(self, item: AdapterListing) -> RemotePreview:
        # the item as built is what is sent: its SKUs are checked again, not only the guard's
        if not_test := live_skus([item.seller_sku, *(unit.seller_sku for unit in item.units())]):
            raise NotEligible(f"{len(not_test)} SKU(s) of the built item are not test SKUs")
        self.verdict = await self.__adapter.preview_remote(item)  # type: ignore[attr-defined]
        return self.verdict

    async def precheck_catalog(self, item: AdapterListing, *, restrictions: bool) -> PrecheckResult:
        check = getattr(self.__adapter, "precheck_catalog", None)
        self.precheck = (
            PrecheckResult()
            if not callable(check)
            else await check(item, restrictions=restrictions)
        )
        return self.precheck

    async def aclose(self) -> None:
        close = getattr(self.__adapter, "aclose", None)
        if close is not None:
            await close()


AdapterOpener = Callable[[uuid.UUID, uuid.UUID, httpx.AsyncBaseTransport], Awaitable[Any]]


async def _open(
    tenant_id: uuid.UUID, account_id: uuid.UUID, transport: httpx.AsyncBaseTransport
) -> Any:
    from app.workers.adapters import open_adapter

    return await open_adapter(tenant_id, account_id, transport=transport, sink=NullSink())


@dataclass
class PreviewDeps:
    """What a run talks through. Production: `open_adapter` over the network; tests: a fake."""

    opener: AdapterOpener = _open
    inner: httpx.AsyncBaseTransport | None = None


def make_preview_deps() -> PreviewDeps:
    """Tests swap this factory for one over a fake adapter or transport."""
    return PreviewDeps()


@dataclass
class SkuRead:
    sku: str
    before: str  # "404" or "found"
    reply: str = "-"
    after: str = "-"


@dataclass
class OneoffReport:
    listing_id: uuid.UUID
    status: str
    precheck: bool
    verdict: RemotePreview | None = None
    skus: list[SkuRead] = field(default_factory=list)
    transport: TransportLog = field(default_factory=TransportLog)
    stop: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return EXIT_STOP if self.stop else EXIT_OK

    def render(self) -> str:
        lines = [
            f"amazon preview: listing {self.listing_id} (status {self.status}), "
            f"pre-check {'on' if self.precheck else 'off'}"
        ]
        verdict = self.verdict
        if verdict is not None:
            lines.append(
                f"verdict: {verdict.status} ({len(verdict.errors)} issue(s)"
                f"{', looked applied' if verdict.applied else ''})"
            )
            for item in verdict.errors:
                lines.append(
                    "issue: "
                    + json.dumps(
                        {
                            "code": item.error_code.value,
                            "amazon_code": item.raw_code,
                            "severity": item.severity.value,
                            "attribute": item.field,
                            "message": item.message,
                        },
                        ensure_ascii=False,
                    )
                )
        else:
            lines.append("verdict: none (no preview was sent)")
        for sku in self.skus:
            lines.append(
                f"sku {sku.sku}: before {sku.before}, preview reply {sku.reply}, after {sku.after}"
            )
        lines.append(f"requests: {self.transport.summary()}")
        if self.stop:
            lines.append(f"STOP: {'; '.join(self.stop)}")
        else:
            lines.append("amazon preview: done (nothing exists on Amazon for these SKUs)")
        return "".join(f"{line}\n" for line in lines)


def live_skus(skus: Sequence[object]) -> list[object]:
    """The SKUs that are not test SKUs (no `KTEST-` prefix, case-sensitive): any one refuses the
    run. A missing or empty SKU is not a test SKU either."""
    return [sku for sku in skus if not (isinstance(sku, str) and sku.startswith(TEST_SKU_PREFIX))]


def sku_refusal(source: PreviewSource, sent: Sequence[str]) -> str | None:
    """Why the listing is not a test listing, or None. Every seller SKU must be a test SKU: the
    product's own, every variant's (sent or not: a family that is not fully test data is not
    previewed) and every SKU the build would send."""
    skus = [source.product.sku, *(child.sku for child in source.children), *sent]
    not_test = live_skus(skus)
    if not not_test:
        return None
    return (
        f"{len(not_test)} SKU(s) of the product and its variants are not test SKUs "
        f"(prefix {TEST_SKU_PREFIX}); the dry run runs on test listings only"
    )


def settings_refusal(settings: Settings, *, precheck: bool) -> str | None:
    """Why the settings refuse the one-off run, or None (module docstring, guard 1)."""
    if not settings.amazon_preview_oneoff:
        return "AMAZON_PREVIEW_ONEOFF is not set (only the one-off staff job sets it)"
    if not settings.amazon_validation_preview_enabled:
        return "AMAZON_VALIDATION_PREVIEW_ENABLED is off"
    if not publish_enabled("amazon", settings):
        return "AMAZON_PUBLISH_ENABLED is off (the SP-API client would refuse the dry run)"
    if precheck != settings.amazon_catalog_precheck_enabled:
        return "AMAZON_CATALOG_PRECHECK_ENABLED must be on exactly when --precheck is passed"
    return None


async def staff_is_admin(email: str) -> bool:
    """The staff email names an active platform admin (read through the login policy)."""
    from app.repositories.identity import UserRepository
    from app.repositories.transactions import identity_session, set_gucs

    async with identity_session() as session:
        await set_gucs(session, login_email=email)
        user = await UserRepository(session).by_email(email)
    return user is not None and user.is_active and user.is_platform_admin


@dataclass
class _Target:
    account_id: uuid.UUID
    version_id: uuid.UUID
    status: str
    skus: list[str]


async def _target(tenant_id: uuid.UUID, listing_id: uuid.UUID, staff_email: str) -> _Target:
    async with tenant_uow(tenant_id) as uow:
        listing = await uow.listings.get(listing_id)
        if listing is None:
            raise NotEligible("no such listing in this tenant")
        if listing.marketplace != "amazon":
            raise NotEligible(f"the listing is for {listing.marketplace}, not amazon")
        status = (
            listing.status
            if listing.status_reason is None
            else (f"{listing.status}/{listing.status_reason}")
        )
        if listing.paused_at is not None or listing.archived_at is not None:
            raise NotEligible("the listing is paused or archived")
        if not previewable(listing, "system", recheck=True):
            raise NotEligible(
                f"status {status}; allowed: validated, needs_review, approved, "
                "blocked/validation_errors"
            )
        if listing.current_version_id is None:
            raise NotEligible("the listing has no current version")
        account = await uow.marketplace_accounts.active_for_publish(
            listing.marketplace, listing.marketplace_account_id
        )
        if account is None:
            raise NotEligible("the listing's Amazon account is not active")
        if await uow.resolution.preview_breaker_open(account):
            raise NotEligible("the account's preview breaker is open (close its review task first)")
        if await uow.job_runs.live_for_listing(listing.id, JobKind.LISTING_STATUS_SYNC) is not None:
            raise NotEligible("a listing_status_sync job of this listing is live; try later")
        # the SKUs the dry run would send, from the listing's product rows: `listing_items` is
        # written by a first submission, so a never-sent listing has none there
        source = await uow.listings.preview_source(listing.id)
        if source is None or source.version is None:
            raise NotEligible("the listing has no current version")
        skus = seller_skus(source)
        refusal = sku_refusal(source, skus)
        if refusal is not None:
            raise NotEligible(refusal)
        if not skus:
            raise NotEligible("the listing has no SKU")
        await uow.audit.write(
            tenant_id=tenant_id,
            actor_type="staff",
            actor_user_id=None,
            actor_email=staff_email,
            action=AUDIT_ACTION,
            entity_type="listing",
            entity_id=listing.id,
            after=json.dumps({"version_id": str(listing.current_version_id), "skus": len(skus)}),
            reason=REASON,
        )
        return _Target(account, listing.current_version_id, status, skus)


async def _read_skus(
    tenant_id: uuid.UUID,
    target: _Target,
    deps: PreviewDeps,
    seen: TransportLog,
    skus: Sequence[str],
) -> dict[str, str]:
    """GET each SKU (getListingsItem): `404` or `found`."""
    from app.adapters.base import AdapterError, AuthExpired

    try:
        adapter = await deps.opener(
            tenant_id, target.account_id, PreviewOnlyTransport(deps.inner, seen)
        )
    except AuthExpired:
        raise AccountInactive(str(target.account_id)) from None
    except AdapterError as exc:
        raise ReadFailed(type(exc).__name__) from None
    reader = ReaderView(adapter)
    try:
        found: dict[str, str] = {}
        for sku in skus:
            result = await reader.get_listing(sku)
            found[sku] = "404" if result is None else "found"
        return found
    finally:
        await reader.aclose()


async def run_oneoff(
    tenant_id: uuid.UUID,
    listing_id: uuid.UUID,
    *,
    staff_email: str,
    precheck: bool,
    deps: PreviewDeps | None = None,
    settings: Settings | None = None,
) -> OneoffReport:
    """The guards, the GETs, the job body's preview and the GETs again (module docstring)."""
    settings = settings or get_settings()
    refusal = settings_refusal(settings, precheck=precheck)
    if refusal is not None:
        raise Refused(refusal)
    if not await staff_is_admin(staff_email):
        raise NotEligible("--staff-email must be an active platform admin")
    deps = deps or make_preview_deps()
    with no_enqueue(REASON):
        target = await _target(tenant_id, listing_id, staff_email)
        report = OneoffReport(listing_id, target.status, precheck)
        seen = report.transport
        before = await _read_skus(tenant_id, target, deps, seen, target.skus)
        report.skus = [SkuRead(sku, state) for sku, state in before.items()]
        exists = [sku for sku, state in before.items() if state == "found"]
        if exists:
            report.stop.append(f"{len(exists)} SKU(s) already exist on Amazon; no preview sent")
            return report
        await _preview(tenant_id, listing_id, target, deps, report)
        verdict = report.verdict
        replies = dict(verdict.per_sku) if verdict is not None else {}
        after = await _read_skus(tenant_id, target, deps, seen, sorted({*target.skus, *replies}))
        rows = {row.sku: row for row in report.skus}
        for sku, state in after.items():
            row = rows.setdefault(sku, SkuRead(sku, "-"))
            row.after = state
            row.reply = replies.get(sku, "-")
        report.skus = [rows[sku] for sku in sorted(rows)]
        _judge(report, after, replies)
        if report.stop:  # Amazon may hold something now: no later dry run until staff look
            async with tenant_uow(tenant_id) as uow:
                await open_breaker(uow, target.account_id)
    log.info(
        "amazon_preview.done",
        listing_id=str(listing_id),
        verdict=None if report.verdict is None else report.verdict.status,
        stop=bool(report.stop),
    )
    return report


async def _preview(
    tenant_id: uuid.UUID,
    listing_id: uuid.UUID,
    target: _Target,
    deps: PreviewDeps,
    report: OneoffReport,
) -> None:
    """The job body, with this run's preview-only adapter as its previewer."""
    wrappers: list[_PreviewOnly] = []

    async def factory(tenant: uuid.UUID, account: uuid.UUID) -> object:
        adapter = await deps.opener(
            tenant, account, PreviewOnlyTransport(deps.inner, report.transport)
        )
        wrapper = _PreviewOnly(adapter)
        wrappers.append(wrapper)
        return wrapper

    ctx = JobContext(
        job_run_id=uuid.uuid4(),
        tenant_id=tenant_id,
        kind=JobKind.LISTING_STATUS_SYNC,
        request_id=new_request_id(),
        payload=dump(
            StatusSyncPayload(
                listing_id=listing_id, op="preview", version_id=target.version_id, trigger="system"
            )
        ),
    )
    previous = previewers.set_previewer_factory(factory)
    try:
        outcome = await run_remote_preview(ctx)  # its persist is deliberately not run
    finally:
        previewers.set_previewer_factory(previous)
    skipped = (outcome.result or {}).get("skipped")
    if skipped is not None:
        raise NotEligible(f"the preview body skipped the listing ({skipped}); nothing was sent")
    wrapper = wrappers[-1] if wrappers else None
    if wrapper is None or wrapper.verdict is None:
        raise RuntimeError("the preview body returned without a verdict")
    verdict = wrapper.verdict
    if wrapper.precheck is not None:
        verdict = with_precheck(verdict, wrapper.precheck)
    report.verdict = verdict


def _judge(report: OneoffReport, after: dict[str, str], replies: dict[str, str]) -> None:
    found = [sku for sku, state in after.items() if state == "found"]
    if found:
        report.stop.append(f"{len(found)} SKU(s) exist on Amazon after the preview")
    # a 4xx is Amazon refusing the request (nothing stored); anything else unknown may be applied
    odd = [
        sku
        for sku, reply in replies.items()
        if reply.upper() not in VERDICTS and not (reply.isdigit() and reply.startswith("4"))
    ]
    if (report.verdict is not None and report.verdict.applied) or odd:
        report.stop.append("a reply was not VALID or INVALID (it may have been applied)")
    if report.transport.refused:
        report.stop.append(f"{len(report.transport.refused)} request(s) refused by the transport")


# ---- the entrypoint ----

EMAIL_RE: Final = re.compile(r"[^@\s]{1,64}@[^@\s]{1,189}")


def _uuid(name: str) -> Callable[[str], uuid.UUID]:
    def parse(value: str) -> uuid.UUID:
        try:
            return uuid.UUID(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"--{name} must be a UUID") from None

    return parse


def _email(value: str) -> str:
    if not EMAIL_RE.fullmatch(value.strip()):
        raise argparse.ArgumentTypeError("--staff-email must be an email address")
    return value.strip().lower()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.workers.amazon_preview")
    parser.add_argument("--tenant-id", type=_uuid("tenant-id"), required=True)
    parser.add_argument("--listing-id", type=_uuid("listing-id"), required=True)
    parser.add_argument("--staff-email", type=_email, required=True)
    parser.add_argument("--precheck", action="store_true")
    return parser


async def _main(args: argparse.Namespace) -> int:
    from app.db.session import dispose_engine

    try:
        with request_context(new_request_id()):
            report = await run_oneoff(
                args.tenant_id,
                args.listing_id,
                staff_email=args.staff_email,
                precheck=args.precheck,
            )
    except Refused as exc:
        sys.stderr.write(f"amazon preview: refused ({exc}); nothing was sent\n")
        return EXIT_REFUSED
    except NotEligible as exc:
        sys.stderr.write(f"amazon preview: not eligible ({exc}); nothing was sent\n")
        return EXIT_NOT_ELIGIBLE
    except (AccountInactive, JobFailed):
        sys.stderr.write("amazon preview: failed (the account must reconnect)\n")
        return EXIT_FAILED
    except (ReadFailed, RetryableJobError):
        sys.stderr.write("amazon preview: failed (Amazon did not answer; run it again later)\n")
        return EXIT_FAILED
    except (PreviewWriteRefused, EnqueueRefused) as exc:
        sys.stdout.write(f"STOP: {type(exc).__name__} (nothing past the refusal was sent)\n")
        return EXIT_STOP
    except Exception as exc:  # the class only, never the message
        sys.stderr.write(f"amazon preview: failed ({type(exc).__name__})\n")
        return EXIT_FAILED
    finally:
        await dispose_engine()
    sys.stdout.write(report.render())
    return report.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(get_settings())
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
