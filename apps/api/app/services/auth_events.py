"""Security events of the sign-in flows (DPP-1, ADR-0021): one audit row and one structured log
line each.

The log line's `message` is the action (`auth.locked`, `auth.mfa_failed`, ...) and it carries the
user and tenant ids and `ip_hash` (the first 16 hex characters of an HMAC-SHA256 of the address
under a key derived from the server's JWT secret: an unkeyed hash of an IPv4 address is reversed
by hashing all 2^32), so a log-based alert can count events per account or address without the
address itself. It never carries the email, a code, a token or a password.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from functools import cache
from typing import Any, Final
from uuid import UUID

from structlog import get_logger

from app.core.config import get_settings
from app.repositories.identity import AuditRepository

log = get_logger(__name__)

IP_HASH_CHARS: Final = 16


IP_HASH_CONTEXT: Final = b"mkos-ip-hash\x00"


@cache
def _process_key() -> bytes:
    """A key for a process with no JWT secret (no tokens are issued there): unlinkable still."""
    return secrets.token_bytes(32)


def _ip_key() -> bytes:
    secret = get_settings().jwt_secret
    if secret is None:
        return _process_key()
    return hashlib.sha256(IP_HASH_CONTEXT + secret.encode("utf-8")).digest()


def ip_hash(ip: str | None) -> str | None:
    if ip is None:
        return None
    return hmac.new(_ip_key(), ip.encode("utf-8"), hashlib.sha256).hexdigest()[:IP_HASH_CHARS]


def log_event(
    action: str,
    *,
    user_id: UUID | None,
    tenant_id: UUID | None,
    ip: str | None,
    reason: str | None = None,
    warning: bool = False,
) -> None:
    """The structured line alone (no audit row), e.g. `auth.attempt_busy`."""
    fields: dict[str, Any] = {
        "user_id": str(user_id) if user_id is not None else None,
        "tenant_id": str(tenant_id) if tenant_id is not None else None,
        "ip_hash": ip_hash(ip),
    }
    if reason is not None:
        fields["reason"] = reason
    if warning:
        log.warning(action, **fields)
    else:
        log.info(action, **fields)


async def record(
    session: Any,
    *,
    action: str,
    user_id: UUID | None,
    tenant_id: UUID | None,
    ip: str | None,
    reason: str | None = None,
    actor_type: str | None = None,
) -> None:
    """The audit row (entity `user`) and its log line. `ip` is the `inet` value or None. The
    actor defaults to the user inside a tenant and to `system` outside one (an event before
    sign-in has no tenant, and `ck_audit_log_platform_actor` allows only system or staff)."""
    if actor_type is None:
        actor_type = "user" if tenant_id is not None and user_id is not None else "system"
    await AuditRepository(session).write(
        tenant_id=tenant_id,
        actor_type=actor_type,
        actor_user_id=user_id if actor_type == "user" else None,
        action=action,
        entity_type="user",
        entity_id=user_id,
        reason=reason,
        ip=ip,
    )
    log_event(action, user_id=user_id, tenant_id=tenant_id, ip=ip, reason=reason)
