"""The limiter dependencies (CANON.md:22, :73; ADR-0005:10-16; CODE-PLAN S10 Q6, C7).

- `limit_ip`: 60/min per client IP; inside every `public()` route.
- `enforce_tenant_limit(tid)`: 120/min per tenant; `app/api/v1/deps.py::limit_tenant` runs
  it after the principal's membership probe passes (the probe's connection is back in the
  pool first), so a refused token never spends the tenant's budget (AUTH-2, TENANCY-2).
- `enforce_refused_principal_limit(tid, sub)`: the same limit on `u:<tid>:<sub>`, counted for a
  verified token whose probe failed (a deactivated member, a suspended tenant).
- `enforce_login_limit(request, email)`: 5/min per `sha256(ip | lower(email))`, called by the
  login (and the sign-up status read) before any password check.
- `enforce_signup_limit(request)`: the same 5/min per `sha256(ip)` alone, called by the public
  sign-up before anything else, so one address cannot mint accounts across many emails
  (ADR-0019).

The client IP (Q6): uvicorn runs with `--proxy-headers --forwarded-allow-ips='*'`, which rewrites
the ASGI peer to the left-most `X-Forwarded-For` entry, so `request.client` is never trusted when
a proxy is in front. `client_ip()` reads the entry `HOPS[APP_ENV]` places from the right of the
raw header: local has no proxy (0: the ASGI peer); the production load balancer appends
`<client>,<lb>` (2: the second from the right); staging is 2 until its first deploy verifies it
(S12). With fewer entries than `hops`, the left-most present entry is used.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final, Literal
from uuid import UUID

from fastapi import Request
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError
from app.core.config import get_settings
from app.services import rate_limit

AppEnv = Literal["local", "staging", "production"]
HOPS: Final[Mapping[AppEnv, int]] = MappingProxyType({"local": 0, "staging": 2, "production": 2})
MAX_IP_CHARS: Final = 64
UNKNOWN_IP: Final = "unknown"


def client_ip(forwarded_for: Sequence[str], peer: str | None, hops: int) -> str:
    """The client address for the limiter; never the left-most entry when a proxy appends one."""
    entries = [e.strip() for header in forwarded_for for e in header.split(",")]
    entries = [e for e in entries if e]
    if hops <= 0 or not entries:
        chosen = peer or UNKNOWN_IP
    elif len(entries) >= hops:
        chosen = entries[-hops]
    else:
        chosen = entries[0]
    return chosen[:MAX_IP_CHARS]


def request_ip(request: Request) -> str:
    peer = request.client.host if request.client is not None else None
    return client_ip(request.headers.getlist("x-forwarded-for"), peer, HOPS[get_settings().app_env])


async def _enforce(bucket: str, limit: int) -> None:
    if await rate_limit.hit(bucket, limit) > limit:
        retry = rate_limit.seconds_left(rate_limit.now())
        raise ApiError(429, ErrorCode.RATE_LIMITED, headers={"Retry-After": str(retry)})


async def limit_ip(request: Request) -> None:
    await _enforce(f"ip:{request_ip(request)}", get_settings().rate_limit_per_ip_per_min)


def login_bucket(ip: str, email: str) -> str:
    digest = hashlib.sha256(f"{ip}|{email.strip().lower()}".encode()).hexdigest()
    return f"login:{digest}"


async def enforce_login_limit(request: Request, email: str) -> None:
    bucket = login_bucket(request_ip(request), email)
    await _enforce(bucket, get_settings().auth_login_rate_limit_per_min)


def signup_bucket(ip: str) -> str:
    return f"signup:{hashlib.sha256(ip.encode()).hexdigest()}"


async def enforce_signup_limit(request: Request) -> None:
    await _enforce(signup_bucket(request_ip(request)), get_settings().auth_login_rate_limit_per_min)


async def enforce_tenant_limit(tenant_id: UUID) -> None:
    await _enforce(f"t:{tenant_id}", get_settings().rate_limit_per_tenant_per_min)


async def enforce_refused_principal_limit(tenant_id: UUID, user_id: UUID) -> None:
    await _enforce(f"u:{tenant_id}:{user_id}", get_settings().rate_limit_per_tenant_per_min)
