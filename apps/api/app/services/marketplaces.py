"""Marketplace accounts: connect, the OAuth callback, confirm, revoke and the list (S15;
BACKEND-ARCHITECTURE.md §3.2; DATABASE.md §5.4-§5.5; invariant 6).

- `start_connect`: a fresh `state` (and, for Flipkart, a PKCE verifier) is stored as its sha256 in
  one tenant transaction; the consent URL goes back to the owner or admin who asked.
- `complete_callback` (public, no JWT): the state row is consumed once by hash, the code is
  exchanged with no transaction open, the tokens are sealed for the state's tenant and stored as a
  *pending* row under a fresh opaque handle. The tenant and user come only from the consumed row.
- `confirm`: the user who started the flow, in its tenant, turns the pending row into the live
  account (or reconnects the same seller); a new account adopts the listings of the tenant's
  revoked accounts for that marketplace. Another seller live in the tenant, or the seller live
  in any other tenant, is 409 `MARKETPLACE_ACCOUNT_CONFLICT`, and the handle survives the
  rollback for a retry after a revoke.
- `revoke`: the stored tokens are deleted (`token_enc` and its key version NULL); the app stays
  authorized at the marketplace until the brand removes it there (D9).

Services seal; they never unseal (the decrypt path is adapter- and worker-only). Token values
stay `SecretStr` and are never logged or put in an error.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from mkos_schemas.codes import ErrorCode

from app.adapters.oauth import (
    CallbackParams,
    OAuthClient,
    OAuthProtocolError,
    OAuthRejectedError,
    OAuthTransientError,
    get_oauth_client,
)
from app.core.config import get_settings
from app.core.crypto import SealedTokens, get_key_wrapper, seal
from app.core.errors import ConflictError, NotFoundError
from app.core.flags import MarketplaceNotEnabled, marketplace_enabled
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.repositories.identity import consume_oauth_state
from app.repositories.transactions import identity_session
from app.repositories.uow import TenantUnitOfWork, tenant_uow

__all__ = [
    "SELLER_CONFLICT_MESSAGE",
    "AccountView",
    "MarketplaceNotConfiguredError",
    "MarketplaceUnavailableError",
    "MarketplaceUnknownError",
    "complete_callback",
    "confirm",
    "list_accounts",
    "revoke",
    "start_connect",
]

log = get_logger(__name__)

SELLER_CONFLICT_MESSAGE: Final = (
    "This seller account already has a live connection; revoke it first."
)
"""The one message for both seller conflicts (D3, Q1): it never names or hints at another tenant."""
AccountStatus = Literal["active", "needs_reconnect", "revoked"]
STATE_BYTES: Final = 32
VERIFIER_BYTES: Final = 48


class MarketplaceNotConfiguredError(Exception):
    """The marketplace's OAuth client (or the token key wrapper) is not configured here: 501."""


class MarketplaceUnavailableError(Exception):
    """The token endpoint failed transiently (5xx, timeout): MARKETPLACE_UNAVAILABLE."""


class MarketplaceUnknownError(Exception):
    """The marketplace's callback was not usable (Amazon without `selling_partner_id`)."""


def bad_state() -> ConflictError:
    """The one answer for a forged, expired, replayed, mismatched or foreign state or handle."""
    return ConflictError(ErrorCode.OAUTH_STATE_INVALID, "oauth_states")


def sha256(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _require_enabled(marketplace: str) -> None:
    """S22 D2, C5: a switched-off marketplace (Flipkart while `FLIPKART_ENABLED=false`) is refused
    with 501 before any client lookup, state write, state consume or token exchange."""
    if not marketplace_enabled(marketplace, get_settings()):
        raise MarketplaceNotEnabled(marketplace)


def _client(marketplace: str) -> OAuthClient:
    client = get_oauth_client(marketplace)
    if client is None:
        raise MarketplaceNotConfiguredError
    try:
        get_key_wrapper()  # the tokens must be sealable before any consent starts
    except RuntimeError:  # no KMS_KEY_URI off local
        raise MarketplaceNotConfiguredError from None
    return client


async def start_connect(principal: Principal, marketplace: str) -> str:
    """The consent URL for `marketplace`; the state row is written in the caller's tenant."""
    _require_enabled(marketplace)
    client = _client(marketplace)
    state = secrets.token_urlsafe(STATE_BYTES)
    verifier = secrets.token_urlsafe(VERIFIER_BYTES) if client.uses_pkce else None
    challenge = s256_challenge(verifier) if verifier is not None else None
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        await uow.oauth_states.insert_state(
            state_hash=sha256(state),
            user_id=principal.user_id,
            marketplace=marketplace,
            code_verifier=verifier,
            ttl_s=get_settings().oauth_state_ttl_seconds,
        )
    return client.authorize_url(state=state, code_challenge=challenge)


async def complete_callback(
    marketplace: str, *, state: str, code: str, selling_partner_id: str | None
) -> str:
    """The OAuth return: consume the state once, exchange the code with no transaction open,
    seal the tokens for the state's tenant and store them under a fresh opaque handle (returned).
    The tenant and user come only from the consumed state row."""
    _require_enabled(marketplace)
    client = _client(marketplace)
    async with identity_session() as session:
        row = await consume_oauth_state(session, state_hash=sha256(state), marketplace=marketplace)
    if row is None:
        raise bad_state()
    if client.marketplace == "amazon" and selling_partner_id is None:
        raise MarketplaceUnknownError
    outcome: Exception | None = None
    try:
        grant = await client.exchange(
            code_verifier=row.code_verifier,
            callback=CallbackParams(code=code, selling_partner_id=selling_partner_id),
        )
    except (OAuthRejectedError, OAuthProtocolError):
        outcome = bad_state()
    except OAuthTransientError:
        outcome = MarketplaceUnavailableError()
    if outcome is not None:
        raise outcome
    sealed = await seal(grant.tokens, tenant_id=row.tenant_id, wrapper=get_key_wrapper())
    handle = secrets.token_urlsafe(STATE_BYTES)
    async with tenant_uow(row.tenant_id, user_id=row.user_id) as uow:
        await uow.oauth_states.insert_pending(
            handle_hash=sha256(handle),
            user_id=row.user_id,
            marketplace=marketplace,
            sealed=sealed,
            grant=grant,
            ttl_s=get_settings().oauth_state_ttl_seconds,
        )
    return handle


@dataclass(frozen=True, slots=True)
class AccountView:
    id: uuid.UUID
    marketplace: str
    seller_id: str
    region: str | None
    status: AccountStatus
    needs_reconnect_since: datetime | None
    expires_at: datetime | None
    created_at: datetime


def _audit_json(status: str) -> str:
    return json.dumps({"status": status})


def _view(row: Any) -> AccountView:
    return AccountView(
        id=row.id,
        marketplace=row.marketplace,
        seller_id=row.seller_id,
        region=row.region,
        status=row.status,
        needs_reconnect_since=row.needs_reconnect_since,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


def seller_conflict() -> ConflictError:
    return ConflictError(ErrorCode.MARKETPLACE_ACCOUNT_CONFLICT, "marketplace_accounts")


async def _adopt_revoked(
    uow: TenantUnitOfWork,
    principal: Principal,
    marketplace: str,
    seller_id: str,
    account_id: uuid.UUID,
) -> None:
    """A new live account replaces the tenant's revoked ones for `marketplace` of the SAME seller
    (DATABASE.md:760; a revoked account of another seller keeps its listings, whose SKUs belong
    to that seller):
    their listings are re-pointed to it with their submission history, any reconnect task still live on them is cancelled
    (`superseded`), and the AUTH_EXPIRED issues a revoke stranded mid-publish are linked to one
    `reconnect_account` task on the new account, applied by this connect, so the publish gate
    and `human_resolution` let the listing publish again."""
    revoked = await uow.marketplace_accounts.revoked_ids(marketplace, seller_id)
    if not revoked:
        return
    for old in revoked:
        await uow.human_tasks.cancel_reconnect(old)
    moved = await uow.listings.repoint_account(marketplace, revoked, account_id)
    # the moved listings take their submission history along (ls_guard 0011b), so the revoked
    # account's purge (DPP 1.7) never deletes it and the publish guards still see every attempt
    history = await uow.submissions.repoint_account(moved, revoked, account_id)
    stranded = await uow.listing_issues.unresolved_auth(moved)
    linked = 0
    if stranded:
        task_id = await uow.human_tasks.applied_by_connect(account_id, principal.user_id)
        if task_id is not None:
            linked = await uow.listing_issues.link_ids(stranded, task_id)
    log.info(
        "marketplace_account.adopted_listings",
        account_id=str(account_id),
        listings=len(moved),
        submissions=history,
        issues_linked=linked,
    )


async def confirm(principal: Principal, pending: str) -> AccountView:
    """Turn the caller's pending connection into the tenant's live account, in one transaction.
    Only the user who started the flow, in its tenant, can confirm it; the same seller reconnects
    the existing account (same id, its reconnect task applied). Any conflict rolls back, so the
    handle stays usable until it expires."""
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        row = await uow.oauth_states.consume_pending(
            handle_hash=sha256(pending), user_id=principal.user_id
        )
        if row is None:
            raise bad_state()
        # S22 C5: raised inside the transaction, so the consume rolls back and the handle survives
        _require_enabled(row.marketplace)
        sealed = SealedTokens(blob=row.token_enc, key_version=row.kms_key_version)
        accounts = uow.marketplace_accounts
        live = await accounts.live_for_update(row.marketplace)
        if live is None:
            created = await accounts.insert_live(
                marketplace=row.marketplace,
                seller_id=row.seller_id,
                region=row.region,
                scopes=row.scopes,
                sealed=sealed,
                expires_at=row.token_expires_at,
                connected_by=principal.user_id,
            )
            if created is not None:
                await _adopt_revoked(uow, principal, row.marketplace, row.seller_id, created.id)
                await uow.audit.write(
                    tenant_id=principal.tenant_id,
                    actor_type="user",
                    actor_user_id=principal.user_id,
                    action="marketplace_account.connected",
                    entity_type="marketplace_account",
                    entity_id=created.id,
                    after=_audit_json(created.status),
                )
                return _view(created)
            # another request of this tenant connected first: treat it as the live account
            live = await accounts.live_for_update(row.marketplace)
            if live is None:
                raise seller_conflict()
        if live.seller_id != row.seller_id:
            raise seller_conflict()
        live_id, before = live.id, live.status  # reconnect() expires the loaded row
        await accounts.reconnect(
            live_id,
            sealed=sealed,
            expires_at=row.token_expires_at,
            region=row.region,
            scopes=row.scopes,
            user_id=principal.user_id,
        )
        await uow.human_tasks.apply_reconnect(live_id, principal.user_id)
        await uow.audit.write(
            tenant_id=principal.tenant_id,
            actor_type="user",
            actor_user_id=principal.user_id,
            action="marketplace_account.reconnected",
            entity_type="marketplace_account",
            entity_id=live_id,
            before=_audit_json(before),
            after=_audit_json("active"),
        )
        return _view(await accounts.reload(live))


async def revoke(principal: Principal, account_id: uuid.UUID) -> AccountView:
    """Delete the stored tokens of one account (D9: not a crypto-shred; the app stays authorized
    at the marketplace until the brand removes it there). Another tenant's id reads as missing.
    An already revoked account is returned unchanged, with no write and no audit row."""
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        accounts = uow.marketplace_accounts
        row = await accounts.get_for_update(account_id)
        if row is None:
            raise NotFoundError("account_id")
        if row.status == "revoked":
            return _view(row)
        row_id, before = row.id, row.status
        await accounts.revoke(row_id)
        await uow.human_tasks.cancel_reconnect(row_id)
        await uow.audit.write(
            tenant_id=principal.tenant_id,
            actor_type="user",
            actor_user_id=principal.user_id,
            action="marketplace_account.revoked",
            entity_type="marketplace_account",
            entity_id=row_id,
            before=_audit_json(before),
            after=_audit_json("revoked"),
        )
        return _view(await accounts.reload(row))


async def list_accounts(
    principal: Principal, *, limit: int, after: tuple[datetime, uuid.UUID] | None
) -> tuple[list[AccountView], bool]:
    """The tenant's accounts, every status, newest first; `status` is the connection health."""
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        rows, has_more = await uow.marketplace_accounts.page(limit=limit, after=after)
        return [_view(row) for row in rows], has_more
