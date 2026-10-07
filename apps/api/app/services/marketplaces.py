"""Marketplace accounts: connect, the OAuth callback, confirm, revoke and the list (S15;
BACKEND-ARCHITECTURE.md §3.2; DATABASE.md §5.4-§5.5; invariant 6).

- `start_connect`: a fresh `state` (and, for Flipkart, a PKCE verifier) is stored as its sha256 in
  one tenant transaction; the consent URL goes back to the owner or admin who asked.
- `complete_callback` (public, no JWT): the state row is consumed once by hash, the code is
  exchanged with no transaction open, the tokens are sealed for the state's tenant and stored as a
  *pending* row under a fresh opaque handle. The tenant and user come only from the consumed row.
- `confirm`: the user who started the flow, in its tenant, turns the pending row into the live
  account (or reconnects the same seller). Another seller live in the tenant, or the seller live
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
from typing import Final

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
from app.core.crypto import get_key_wrapper, seal
from app.core.errors import ConflictError
from app.core.rbac import Principal
from app.repositories.identity import consume_oauth_state
from app.repositories.transactions import identity_session
from app.repositories.uow import tenant_uow

__all__ = [
    "MarketplaceNotConfiguredError",
    "MarketplaceUnavailableError",
    "MarketplaceUnknownError",
    "complete_callback",
    "start_connect",
]

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


def _client(marketplace: str) -> OAuthClient:
    client = get_oauth_client(marketplace)
    if client is None:
        raise MarketplaceNotConfiguredError
    try:
        get_key_wrapper()  # the tokens must be sealable before any consent starts
    except NotImplementedError:
        raise MarketplaceNotConfiguredError from None
    return client


async def start_connect(principal: Principal, marketplace: str) -> str:
    """The consent URL for `marketplace`; the state row is written in the caller's tenant."""
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
    status: str
    needs_reconnect_since: datetime | None
    expires_at: datetime | None
    created_at: datetime


def _audit_json(status: str) -> str:
    return json.dumps({"status": status})
