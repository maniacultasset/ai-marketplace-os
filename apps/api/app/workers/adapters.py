"""`open_adapter`: one marketplace adapter session for one tenant's account (S20 T15; AC10,
AC12, AC13; C15, C17; invariants 1, 5, 6).

The only way into a mutating adapter (`.importlinter` `marketplace-io-only-in-adapters-and-
workers`): S21's publish job calls it, after approval (invariant 1).

- The account is read in the tenant's `unit_of_work` (RLS): another tenant's id is
  `AccountNotFound`; `needs_reconnect` or `revoked` is `AuthExpired` (nothing sent).
- The SP-API endpoint is checked against `AMAZON_USE_SANDBOX` in every environment (C17) before
  any unseal or call: a mismatch is a definitive `MarketplaceUnavailable` naming the variables.
- The seller id comes from the account row only, never from listing data.
- `WorkerTokenSource` unseals in memory per call to `get()` (cached for the session), never inside
  a transaction that spans HTTP; the first unseal of a session writes one `credential_used`
  audit row, and a forced refresh (S15's `refresh_account(force=True)`) writes its own (C15).
- The limiter is `PgOutboundLimiter` (`mkt:` buckets), the error map the enabled rows, the
  transport `make_transport()` (None in the app; tests pass the fake).
- Flipkart has no adapter until S22 (`NotImplementedError`).
"""

from __future__ import annotations

import uuid
from typing import Final

import httpx
from mkos_schemas.codes import ErrorCode
from pydantic import SecretStr

from app.adapters.amazon import constants as amazon_constants
from app.adapters.amazon.adapter import AmazonAdapter
from app.adapters.amazon.client import SpApiClient, failure
from app.adapters.amazon.endpoint import spapi_endpoint_errors
from app.adapters.base import AuthExpired, MarketplaceUnavailable, RawResponseSink
from app.adapters.credentials import CredentialUnsealError, get_key_unwrapper, unseal
from app.adapters.error_map import ErrorMapper
from app.adapters.oauth.http import make_transport
from app.core.config import get_settings
from app.repositories.uow import tenant_uow
from app.workers.outbound_limiter import PgOutboundLimiter
from app.workers.token_refresh import REFRESH_MARGIN, refresh_account

ENTITY: Final = "marketplace_account"


class AccountNotFound(LookupError):  # noqa: N818 - the plan's name (S20 T15)
    """No such account in this tenant."""


class WorkerTokenSource:
    """The access token of one account for one adapter session."""

    def __init__(self, tenant_id: uuid.UUID, account_id: uuid.UUID) -> None:
        self._tenant = tenant_id
        self._account = account_id
        self._token: SecretStr | None = None
        self._audited = False

    def __repr__(self) -> str:
        return "WorkerTokenSource()"

    async def _read(self, *, audit: bool) -> SecretStr:
        async with tenant_uow(self._tenant) as uow:
            snap = await uow.marketplace_accounts.snapshot(self._account, margin=REFRESH_MARGIN)
            if (
                snap is None
                or snap.status != "active"
                or snap.token_enc is None
                or snap.kms_key_version is None
            ):
                raise AuthExpired(failure(ErrorCode.AUTH_EXPIRED, "The account must reconnect."))
            blob, version = snap.token_enc, snap.kms_key_version
            if audit:
                await uow.audit.write(
                    tenant_id=self._tenant,
                    actor_type="system",
                    actor_user_id=None,
                    action="marketplace_account.credential_used",
                    entity_type=ENTITY,
                    entity_id=self._account,
                    reason="marketplace_call",
                )
        try:
            pair = await unseal(
                blob, key_version=version, tenant_id=self._tenant, unwrapper=get_key_unwrapper()
            )
        except CredentialUnsealError:
            raise MarketplaceUnavailable(
                failure(
                    ErrorCode.MARKETPLACE_UNKNOWN_ERROR,
                    "The stored credential could not be opened.",
                )
            ) from None
        return pair.access_token

    async def get(self) -> SecretStr:
        if self._token is None:
            self._token = await self._read(audit=not self._audited)
            self._audited = True
        return self._token

    async def refresh(self) -> SecretStr:
        outcome = await refresh_account(self._tenant, self._account, force=True)
        if outcome in ("rejected", "stale"):
            raise AuthExpired(failure(ErrorCode.AUTH_EXPIRED, "Amazon refused the token refresh."))
        if outcome in ("transient", "unseal_failed"):
            raise MarketplaceUnavailable(
                failure(ErrorCode.MARKETPLACE_UNAVAILABLE, "The token refresh did not complete.")
            )
        self._token = await self._read(audit=False)  # the refresh audited its own use
        return self._token


async def open_adapter(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sink: RawResponseSink | None = None,
) -> AmazonAdapter:
    """`sink` (S21 CP12) keeps every raw marketplace body for the submission ledger."""
    settings = get_settings()
    async with tenant_uow(tenant_id) as uow:
        account = await uow.marketplace_accounts.active_by_id(account_id)
        rows = await uow.error_map.enabled_rows(account.marketplace) if account else []
    if account is None:
        raise AccountNotFound(str(account_id))
    if account.marketplace != "amazon":
        raise NotImplementedError("the Flipkart adapter lands with S22")
    if account.status != "active":
        raise AuthExpired(failure(ErrorCode.AUTH_EXPIRED, "The account must reconnect."))
    problems = spapi_endpoint_errors(
        settings.amazon_spapi_endpoint,
        use_sandbox=settings.amazon_use_sandbox,
        app_env=settings.app_env,
    )
    if problems:
        raise MarketplaceUnavailable(
            failure(ErrorCode.MARKETPLACE_UNAVAILABLE, "; ".join(problems))
        )
    client = SpApiClient(
        endpoint=settings.amazon_spapi_endpoint,
        marketplace_id=settings.amazon_marketplace_id,
        seller_id=account.seller_id,
        tokens=WorkerTokenSource(tenant_id, account_id),
        limiter=PgOutboundLimiter("amazon", account_id, rates=amazon_constants.RATES),
        transport=transport or make_transport(),
    )
    return AmazonAdapter(
        client, ErrorMapper(rows), sink, marketplace_id=settings.amazon_marketplace_id
    )
