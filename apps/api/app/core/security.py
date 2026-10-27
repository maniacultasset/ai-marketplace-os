"""Access and refresh tokens (ADR-0005:30; ADR-0006; ENV.md JWT rows; CANON.md:21).

- `encode` signs a `JWTClaims` with `JWT_SECRET`, HS256 only.
- `decode` accepts exactly `algorithms=["HS256"]`, requires `exp`, `iat`, `iss`, `aud`, `sub` and
  `jti`, checks the issuer and audience from settings, validates the payload as the strict
  `JWTClaims` (so a missing or extra claim fails), and refuses the other `typ`. Every failure is
  the one `InvalidToken("invalid token")`, raised with no cause, so neither the token, a claim
  nor the secret can reach a log line or a response through it.
- Refresh tokens are signed JWTs too (PLAN Decision 8); the database keeps only `token_hash`.
- DPP-1 (contract 2.0.0, ADR-0021): `amr`, `mfa_at` and `sid` are optional claims. `encode` drops
  the unset ones, so a token minted under `MFA_ENFORCEMENT=off` has exactly the 10 frozen claims
  and an older API (whose `JWTClaims` forbids extra keys) still accepts it.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Final, Literal

import jwt
from mkos_schemas.auth import AuthMethod, JWTClaims, Role
from pydantic import ValidationError

from app.core.config import get_settings

ALGORITHM: Final = "HS256"
REQUIRED_CLAIMS: Final = ("exp", "iat", "iss", "aud", "sub", "jti")
MAX_TOKEN_CHARS: Final = 4096
TokenType = Literal["access", "refresh"]


class InvalidToken(Exception):  # noqa: N818 - CODE-PLAN name
    """Any token that fails signature, algorithm, claim, type or shape checks."""

    def __init__(self) -> None:
        super().__init__("invalid token")


def _secret() -> str:
    secret = get_settings().jwt_secret
    if secret is None:
        raise RuntimeError("JWT_SECRET is required to sign or verify tokens")
    return secret


def new_jti() -> str:
    return str(uuid.uuid4())


def token_hash(raw: str) -> bytes:
    """sha256 of the raw token: what `refresh_tokens.token_hash` stores."""
    return hashlib.sha256(raw.encode("utf-8")).digest()


def issue_claims(
    *,
    sub: uuid.UUID,
    tid: uuid.UUID,
    role: Role,
    email: str,
    typ: TokenType,
    amr: list[AuthMethod] | None = None,
    mfa_at: int | None = None,
    sid: uuid.UUID | None = None,
) -> JWTClaims:
    """Fresh claims with the configured issuer, audience and lifetime for `typ`."""
    settings = get_settings()
    now = int(time.time())
    ttl = (
        settings.access_token_ttl_minutes * 60
        if typ == "access"
        else settings.refresh_token_ttl_days * 24 * 3600
    )
    return JWTClaims(
        sub=sub,
        tid=tid,
        roles=[role],
        email=email,
        typ=typ,
        jti=new_jti(),
        iss=settings.jwt_issuer,
        aud=settings.jwt_audience,
        iat=now,
        exp=now + ttl,
        amr=amr,
        # never after `iat` (a code entered within this same second, or a clock a step ahead)
        mfa_at=None if mfa_at is None else min(mfa_at, now),
        sid=sid,
    )


def encode(claims: JWTClaims) -> str:
    payload = claims.model_dump(mode="json", exclude_none=True)
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM)


def _verified_payload(token: str) -> object:
    settings = get_settings()
    return jwt.decode(
        token,
        _secret(),
        algorithms=[ALGORITHM],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
        options={"require": list(REQUIRED_CLAIMS)},
    )


def decode(token: str, *, typ: TokenType) -> JWTClaims:
    """The verified claims of `token`, or `InvalidToken`."""
    claims: JWTClaims | None = None
    if isinstance(token, str) and 0 < len(token) <= MAX_TOKEN_CHARS:
        try:
            claims = JWTClaims.model_validate(_verified_payload(token))
        except (jwt.PyJWTError, ValidationError, ValueError, TypeError):
            claims = None
    # Raised outside the `except` block: no __context__, so nothing about the token is chained.
    if claims is None or claims.typ != typ:
        raise InvalidToken
    return claims
