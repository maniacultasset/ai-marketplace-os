"""The marketplace token refresh (S15; DW5; BACKEND-ARCHITECTURE.md §3.2; spec L5262-5263, L5360).

`refresh_account(tenant_id, account_id)` refreshes one account's token pair before it expires. It
lives in the worker because it unseals (EXECUTION-PLAN decision 15: only adapters and workers
reach the decrypt path), and it holds no transaction across the marketplace call:

1. `refresh_lock` (a try-advisory lock): held elsewhere (the periodic pass and an on-demand
   refresh racing) -> `skipped`.
2. A snapshot in one short transaction; not active or not due -> `skipped`.
3. Unseal and `client.refresh` with no transaction open.
4. One post-HTTP transaction writes the outcome with a compare-and-set on `status = 'active' AND
   updated_at = <seen>`: `refreshed` (new sealed tokens), `rejected` (`invalid_grant`: the account
   goes `needs_reconnect` with `AUTH_EXPIRED` and one `reconnect_account` task opens) or
   `transient` (a 5xx, timeout or unusable reply: the error code is recorded, the account stays
   active and is retried next pass). Whatever the CAS returns, that transaction writes exactly
   one `credential_used` audit row (actor `system`), since the credential was used. A CAS miss
   (a revoke or reconnect landed meanwhile) is `stale` and changes nothing else.
5. After the commit, only when the account went `needs_reconnect`: the `token_refresh_failed`
   alert (S23: a WARNING `alert.token_refresh_failed` log event, one Sentry message and the Slack
   post) with the tenant, account, marketplace and `AUTH_EXPIRED`, once per account per UTC day.

`force=True` (S20 T14; D9, C3) is the adapter's "the marketplace refused this token" refresh: it
skips only the due check (lock, snapshot and CAS unchanged). If another refresh holds the lock,
it waits once for that lock to free (polling, at most `FORCE_WAIT`) and reports `skipped`, so
the caller re-reads the token the other refresh wrote.

A blob that will not unseal is recorded as `MARKETPLACE_UNKNOWN_ERROR` with one ERROR event
`marketplace.credential_unseal_failed`. Plaintext tokens live only in this function's locals and
never reach a logger.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from mkos_schemas.codes import ErrorCode

from app.adapters.credentials import CredentialUnsealError, get_key_unwrapper, unseal
from app.adapters.oauth import (
    OAuthProtocolError,
    OAuthRejectedError,
    OAuthTransientError,
    TokenGrant,
    get_oauth_client,
)
from app.core.alerts import day_window
from app.core.crypto import SealedTokens, get_key_wrapper, seal
from app.core.logging import get_logger
from app.repositories.marketplace_accounts import refresh_lock
from app.repositories.uow import tenant_uow
from app.workers.alerting import alert

log = get_logger(__name__)

RefreshOutcome = Literal["refreshed", "rejected", "transient", "skipped", "stale", "unseal_failed"]
REFRESH_MARGIN: Final = timedelta(minutes=15)
"""Refresh when the access token expires within this window (the periodic pass runs every 5)."""
ENTITY: Final = "marketplace_account"
FORCE_WAIT: Final = 10.0
"""How long a forced refresh waits for another holder of the account's refresh lock."""
FORCE_POLL: Final = 0.2


async def refresh_account(
    tenant_id: uuid.UUID, account_id: uuid.UUID, *, force: bool = False
) -> RefreshOutcome:
    """Refresh one account of `tenant_id` if it is active and due (or, with `force`, active);
    the outcome."""
    async with refresh_lock(account_id) as held:
        if held:
            return await _refresh_locked(tenant_id, account_id, force=force)
    if force:
        await _wait_for_lock(account_id)
    return "skipped"


async def _wait_for_lock(account_id: uuid.UUID) -> None:
    """Poll until the other refresh released the lock, at most `FORCE_WAIT` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + FORCE_WAIT
    while loop.time() < deadline:
        await asyncio.sleep(FORCE_POLL)
        async with refresh_lock(account_id) as held:
            if held:
                return


async def _refresh_locked(
    tenant_id: uuid.UUID, account_id: uuid.UUID, *, force: bool = False
) -> RefreshOutcome:
    async with tenant_uow(tenant_id) as uow:
        snap = await uow.marketplace_accounts.snapshot(account_id, margin=REFRESH_MARGIN)
    if snap is None or snap.token_enc is None or snap.kms_key_version is None:
        return "skipped"
    if snap.status != "active" or not (snap.due or force):
        return "skipped"
    client = get_oauth_client(snap.marketplace)
    if client is None:
        log.warning(
            "marketplace.token_refresh_unconfigured",
            tenant_id=str(tenant_id),
            account_id=str(account_id),
            marketplace=snap.marketplace,
        )
        return "skipped"
    try:
        tokens = await unseal(
            snap.token_enc,
            key_version=snap.kms_key_version,
            tenant_id=tenant_id,
            unwrapper=get_key_unwrapper(),
        )
    except CredentialUnsealError:
        async with tenant_uow(tenant_id) as uow:
            await uow.marketplace_accounts.cas_error(
                account_id, snap.updated_at, ErrorCode.MARKETPLACE_UNKNOWN_ERROR.value
            )
        log.error(
            "marketplace.credential_unseal_failed",
            tenant_id=str(tenant_id),
            account_id=str(account_id),
            marketplace=snap.marketplace,
            error_code=ErrorCode.MARKETPLACE_UNKNOWN_ERROR.value,
        )
        return "unseal_failed"

    outcome: RefreshOutcome
    grant: TokenGrant | None = None
    error_code: ErrorCode | None = None
    try:
        grant = await client.refresh(tokens.refresh_token)
        outcome = "refreshed"
    except OAuthRejectedError:
        outcome = "rejected"
    except OAuthTransientError:
        outcome, error_code = "transient", ErrorCode.MARKETPLACE_UNAVAILABLE
    except OAuthProtocolError:
        outcome, error_code = "transient", ErrorCode.MARKETPLACE_UNKNOWN_ERROR
    del tokens

    sealed: SealedTokens | None = None
    if grant is not None:
        sealed = await seal(grant.tokens, tenant_id=tenant_id, wrapper=get_key_wrapper())

    async with tenant_uow(tenant_id) as uow:
        accounts = uow.marketplace_accounts
        if grant is not None and sealed is not None:
            hit = await accounts.cas_tokens(account_id, snap.updated_at, sealed, grant.expires_at)
        elif outcome == "rejected":
            hit = await accounts.cas_needs_reconnect(account_id, snap.updated_at)
            if hit:
                await uow.human_tasks.open_reconnect(account_id)
                await uow.audit.write(
                    tenant_id=tenant_id,
                    actor_type="system",
                    actor_user_id=None,
                    action="marketplace_account.needs_reconnect",
                    entity_type=ENTITY,
                    entity_id=account_id,
                    before='{"status": "active"}',
                    after='{"status": "needs_reconnect"}',
                )
        else:
            code = error_code or ErrorCode.MARKETPLACE_UNKNOWN_ERROR
            hit = await accounts.cas_error(account_id, snap.updated_at, code.value)
        # the credential was unsealed and used: audited whatever the CAS returned (spec L12629)
        await uow.audit.write(
            tenant_id=tenant_id,
            actor_type="system",
            actor_user_id=None,
            action="marketplace_account.credential_used",
            entity_type=ENTITY,
            entity_id=account_id,
            reason="token_refresh",
        )
    if not hit:
        return "stale"
    if outcome == "rejected":
        # S23: the alert event (WARNING log, Sentry message, Slack), once per account per UTC day
        now = datetime.now(UTC)
        await alert(
            "token_refresh_failed",
            key=str(account_id),
            window_start=day_window(now),
            now=now,
            tenant_id=tenant_id,
            account_id=account_id,
            marketplace=snap.marketplace,
            error_code=ErrorCode.AUTH_EXPIRED.value,
        )
    return outcome
