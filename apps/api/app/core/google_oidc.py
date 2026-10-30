"""Google ID tokens and the authorization request (ADR-0020): pure functions, no network.

This is the only RS256 verifier in the backend. ADR-0020 amends RULES §4 and ADR-0005 for it alone:
Kuinn's own tokens stay HS256 (`app.core.security`). The HTTP calls (the code exchange and the
JWKS download) live in `app.adapters.oauth.google`; the keys are cached in `JwksCache` here.

- PKCE S256 (RFC 7636): a 64-character verifier, the challenge is its base64url sha256.
- The browser binding: the browser keeps a random secret and sends `start` only
  `b = base64url(sha256(secret))`; `complete` sends the secret, whose sha256 must match.
- `verify_id_token`: RS256 only (`none` and HS256 are refused before any key is read), a known
  `kid`, `iss` Google's issuer (with or without the scheme, as Google documents), `aud` the client
  id, `azp` the client id when present, `exp`, `iat` (10 s leeway) and `sub` required, the nonce's
  sha256 equal to the one stored at `start`, `email_verified` exactly true. Email, `hd` and the
  names are read from the verified claims only and never logged.
- `is_authoritative`: Google owns the address only for `@gmail.com` (and `@googlemail.com`) or a
  Workspace account whose `hd` is the email's own domain; any other Google account (an external
  email registered at Google) is refused with the one generic failure.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlencode

import jwt

SCOPES: Final = "openid email profile"
ALGORITHM: Final = "RS256"
LEEWAY_SECONDS: Final = 10
JWKS_TTL_SECONDS: Final = 3600
JWKS_MISS_REFETCH_SECONDS: Final = 60
VERIFIER_BYTES: Final = 48  # 64 base64url characters (RFC 7636: 43..128)
STATE_BYTES: Final = 32
BINDING_CHARS: Final = 43  # base64url of a sha256, no padding
GMAIL_DOMAINS: Final = frozenset({"gmail.com", "googlemail.com"})
NAME_MAX: Final = 200
SUB_MAX: Final = 255
EMAIL_OCTETS_MAX: Final = 254


class InvalidIdToken(Exception):  # noqa: N818 - a refusal with a reason code, never the token
    """The ID token failed one check. `reason` is a fixed code; the message never holds a claim
    or the token."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnknownKey(InvalidIdToken):
    """The token's `kid` is not in the cached key set: the caller may refetch the keys once."""

    def __init__(self) -> None:
        super().__init__("unknown_kid")


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sha256(text: str) -> bytes:
    return hashlib.sha256(text.encode("ascii")).digest()


def new_token(n_bytes: int = STATE_BYTES) -> str:
    """An opaque single-use value (state, nonce, a handle): 32 random bytes, base64url."""
    return secrets.token_urlsafe(n_bytes)


def new_verifier() -> str:
    return secrets.token_urlsafe(VERIFIER_BYTES)


def s256(verifier: str) -> str:
    return b64url(sha256(verifier))


def binding_hash(b: str) -> bytes | None:
    """The 32-byte sha256 the browser sent to `start` as `b`, or None when malformed."""
    if len(b) != BINDING_CHARS or not set(b) <= _B64URL:
        return None
    raw = b64url_decode(b)
    return raw if len(raw) == 32 and b64url(raw) == b else None


def binding_matches(secret: str, stored: bytes) -> bool:
    """True when `secret` (the browser's raw binding) hashes to the stored binding."""
    try:
        digest = sha256(secret)
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(digest, stored)


_B64URL: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def authorize_url(
    base: str, *, client_id: str, redirect_uri: str, state: str, nonce: str, verifier: str
) -> str:
    """Google's consent URL: the authorization code flow with PKCE S256, `state`, `nonce` and
    `prompt=select_account`, scopes `openid email profile` only (no photo is stored or shown)."""
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "nonce": nonce,
            "code_challenge": s256(verifier),
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
    )
    return f"{base}?{query}"


@dataclass(frozen=True, slots=True)
class Identity:
    """The verified Google identity. Never logged (`repr` hides the personal fields)."""

    sub: str = field(repr=False)
    email: str = field(repr=False)
    hd: str | None = field(repr=False)
    display_name: str | None = field(repr=False)
    given_name: str | None = field(repr=False)
    family_name: str | None = field(repr=False)

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[1]


def is_authoritative(email: str, hd: str | None) -> bool:
    """Google owns this address: a consumer Gmail address, or a Workspace account whose hosted
    domain is the address's own domain."""
    if email.count("@") != 1:
        return False
    domain = email.rsplit("@", 1)[1].lower()
    if domain in GMAIL_DOMAINS:
        return True
    return hd is not None and hd.lower() == domain


def _issuers(issuer: str) -> frozenset[str]:
    """Google documents both `https://accounts.google.com` and `accounts.google.com`."""
    bare = issuer.removeprefix("https://")
    return frozenset({issuer, bare, f"https://{bare}"})


def _name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())[:NAME_MAX]
    return cleaned or None


def verify_id_token(
    token: str,
    keys: Mapping[str, jwt.PyJWK],
    *,
    client_id: str,
    issuer: str,
    nonce_hash: bytes,
    now: float | None = None,
) -> Identity:
    """The verified identity, or `InvalidIdToken` with a reason code (`UnknownKey` for a `kid`
    missing from `keys`)."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise InvalidIdToken("malformed") from None
    if header.get("alg") != ALGORITHM:
        raise InvalidIdToken("algorithm")
    kid = header.get("kid")
    if not isinstance(kid, str) or kid not in keys:
        raise UnknownKey
    moment = time.time() if now is None else now
    try:
        claims = jwt.decode(
            token,
            key=keys[kid].key,
            algorithms=[ALGORITHM],
            audience=client_id,
            leeway=LEEWAY_SECONDS,
            options={
                "require": ["iss", "aud", "sub", "exp", "iat"],
                # checked below: the issuer against both documented forms, the times against
                # one clock with the 10 s leeway
                "verify_iss": False,
                "verify_exp": False,
                "verify_iat": False,
            },
        )
    except jwt.InvalidAudienceError:
        raise InvalidIdToken("audience") from None
    except jwt.PyJWTError:
        raise InvalidIdToken("signature_or_claims") from None
    return _identity(claims, client_id=client_id, issuer=issuer, nonce_hash=nonce_hash, now=moment)


def _identity(
    claims: Mapping[str, Any], *, client_id: str, issuer: str, nonce_hash: bytes, now: float
) -> Identity:
    if claims.get("iss") not in _issuers(issuer):
        raise InvalidIdToken("issuer")
    azp = claims.get("azp")
    if azp is not None and azp != client_id:
        raise InvalidIdToken("azp")
    exp, iat = claims.get("exp"), claims.get("iat")
    if not isinstance(exp, int | float) or not isinstance(iat, int | float):
        raise InvalidIdToken("signature_or_claims")
    if isinstance(exp, bool) or isinstance(iat, bool):
        raise InvalidIdToken("signature_or_claims")
    if exp + LEEWAY_SECONDS < now:
        raise InvalidIdToken("expired")
    if iat > now + LEEWAY_SECONDS:
        raise InvalidIdToken("not_yet_valid")
    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not nonce.isascii():
        raise InvalidIdToken("nonce")
    if not hmac.compare_digest(sha256(nonce), nonce_hash):
        raise InvalidIdToken("nonce")
    if claims.get("email_verified") is not True:
        raise InvalidIdToken("email_unverified")
    sub, email, hd = claims.get("sub"), claims.get("email"), claims.get("hd")
    if not isinstance(sub, str) or not 1 <= len(sub) <= SUB_MAX:
        raise InvalidIdToken("sub")
    if (
        not isinstance(email, str)
        or not email.isascii()
        or email.count("@") != 1
        or len(email) > EMAIL_OCTETS_MAX
        or email != email.strip()
        or any(ch.isspace() for ch in email)
    ):
        raise InvalidIdToken("email")
    if hd is not None and (not isinstance(hd, str) or not hd.isascii() or len(hd) > 253):
        raise InvalidIdToken("hd")
    given, family = _name(claims.get("given_name")), _name(claims.get("family_name"))
    display = _name(claims.get("name")) or _name(" ".join(p for p in (given, family) if p))
    return Identity(
        sub=sub,
        email=email.lower(),
        hd=hd.lower() if isinstance(hd, str) else None,
        display_name=display,
        given_name=given,
        family_name=family,
    )


def parse_jwks(document: Any) -> dict[str, jwt.PyJWK]:
    """The RSA signing keys of a JWKS document by `kid` (other keys are skipped)."""
    if not isinstance(document, Mapping) or not isinstance(document.get("keys"), list):
        raise ValueError("not a JWKS document")
    found: dict[str, jwt.PyJWK] = {}
    for entry in document["keys"]:
        if not isinstance(entry, Mapping) or entry.get("kty") != "RSA":
            continue
        kid = entry.get("kid")
        if not isinstance(kid, str) or entry.get("use", "sig") != "sig":
            continue
        try:
            found[kid] = jwt.PyJWK(dict(entry), algorithm=ALGORITHM)
        except jwt.PyJWTError:
            continue
    return found


def jwks_from_bytes(body: bytes) -> dict[str, jwt.PyJWK]:
    return parse_jwks(json.loads(body))


@dataclass
class JwksCache:
    """Google's keys for an hour; a token with an unknown `kid` may refetch them, at most once a
    minute (Google rotates keys with overlap, so a miss is either a rotation or a forgery)."""

    clock: Callable[[], float] = time.monotonic
    keys: dict[str, jwt.PyJWK] = field(default_factory=dict)
    fetched_at: float | None = None
    last_miss_refetch: float | None = None

    def fresh(self) -> bool:
        return self.fetched_at is not None and self.clock() - self.fetched_at < JWKS_TTL_SECONDS

    def may_refetch_for_miss(self) -> bool:
        now = self.clock()
        if self.last_miss_refetch is not None and now - self.last_miss_refetch < (
            JWKS_MISS_REFETCH_SECONDS
        ):
            return False
        self.last_miss_refetch = now
        return True

    def store(self, keys: dict[str, jwt.PyJWK]) -> None:
        self.keys = keys
        self.fetched_at = self.clock()


__all__ = [
    "Identity",
    "InvalidIdToken",
    "JwksCache",
    "UnknownKey",
    "authorize_url",
    "b64url",
    "binding_hash",
    "binding_matches",
    "is_authoritative",
    "jwks_from_bytes",
    "new_token",
    "new_verifier",
    "parse_jwks",
    "s256",
    "sha256",
    "verify_id_token",
]
