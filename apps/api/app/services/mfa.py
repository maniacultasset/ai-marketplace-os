"""TOTP factors, recovery codes and sign-in challenges (DPP-1, ADR-0021).

The only importer of `app.adapters.mfa_secrets` (import contract
`mfa-secret-unseal-only-in-mfa-service`): a TOTP secret is sealed with `seal_bytes` under the
`mfa-secret-key` wrapper and opened here, for one `match_step`, and nowhere else.
"""

from __future__ import annotations

from uuid import UUID

from app.adapters.mfa_secrets import unseal_bytes
from app.core.crypto import (
    MFA_BLOB_MAGIC,
    SealedBytes,
    get_mfa_key_wrapper,
    mfa_aad,
    seal_bytes,
)


async def seal_secret(secret: bytes, *, user_id: UUID) -> SealedBytes:
    """A new TOTP secret sealed for `user_id` under the MFA key."""
    return await seal_bytes(
        secret,
        aad=mfa_aad(user_id),
        subject=user_id,
        wrapper=get_mfa_key_wrapper(),
        magic=MFA_BLOB_MAGIC,
    )


async def open_secret(blob: bytes, key_version: str, *, user_id: UUID) -> bytes:
    """The TOTP secret sealed for `user_id`; any failure is `MfaUnsealError`."""
    return await unseal_bytes(blob, key_version, user_id=user_id)
