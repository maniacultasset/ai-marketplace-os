"""The limiter dependencies (CANON.md:22, :73; ADR-0005:10-16; CODE-PLAN S10 Q6, C7).

- `limit_ip`: 60/min per client IP; inside every `public()` route.
- `enforce_tenant_limit(tid)`: 120/min per tenant; `app/api/v1/deps.py::limit_tenant` runs
  it after the principal's membership probe passes (the probe's connection is back in the
  pool first), so a refused token never spends the tenant's budget (AUTH-2, TENANCY-2).
- `enforce_refused_principal_limit(tid, sub)`: the same limit on `u:<tid>:<sub>`, counted for a
  verified token whose probe failed (a deactivated member, a suspended tenant).
- `enforce_login_limit(request, email)`: 5/min per `sha256(ip | lower(email))`, plus 10/min per
  `sha256(lower(email))` from any address (so rotating addresses cannot multiply the guesses on
  one account), called by the login (and the sign-up status read) before any password check.
- `enforce_signup_limit(request)`: the same 5/min per `sha256(ip)` alone, called by the public
  sign-up before anything else, so one address cannot mint accounts across many emails
  (ADR-0019).
- `enforce_signup_poll_limit(request, handle)`: the sign-up status poll (ADR-0019 amendment),
  per `sha256(ip|handle)` and, at a higher ceiling, per `sha256(ip)`.
- `enforce_google_limit(request)`: `AUTH_GOOGLE_RATE_LIMIT_PER_MIN` (10) per `sha256(ip)` on
  every Google sign-in step (ADR-0020); the JSON steps then add the login limiter keyed on the
  email their handle carries, before the handle is consumed.

The client IP (Q6): uvicorn runs with `--proxy-headers --forwarded-allow-ips='*'`, which rewrites
the ASGI peer to the left-most `X-Forwarded-For` entry, so `request.client` is never trusted when
a proxy is in front. `client_ip()` reads the entry `HOPS[APP_ENV]` places from the right of the
raw header: local has no proxy (0: the ASGI peer); staging and production take traffic straight
on the `run.app` URL, where Google's front end appends only the client address (1: the right-most
entry; a caller-written entry is always to its left). Put a load balancer in front and this becomes
2 (`<client>,<lb>`) in the same change. With fewer entries than `hops`, the left-most present entry
is used. The access log carries the entry count as `xff_entries` (never the values), which is how
the cutover measures it; `HOPS["production"]` changes only in the cutover PR
(docs/runbooks/EDGE-CUTOVER.md steps 4-5; KNOWN-ISSUES.md S25 hop counts).

Behind the interim Cloudflare edge (ADR-0025) the Worker sends the client address it read from
`cf-connecting-ip` as `x-kuinn-client-ip`; `edge_client_ip()` trusts it only on a request whose
origin secret matched, and `request_ip()` falls back to `client_ip()` otherwise.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final, Literal
from uuid import UUID

from fastapi import Request
from mkos_schemas.codes import ErrorCode
from starlette.types import Scope

from app.api.errors import ApiError
from app.api.origin_auth import CLIENT_IP_HEADER, SCOPE_KEY
from app.core.config import get_settings
from app.services import rate_limit

AppEnv = Literal["local", "staging", "production"]
HOPS: Final[Mapping[AppEnv, int]] = MappingProxyType({"local": 0, "staging": 1, "production": 1})
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


def edge_client_ip(scope: Scope) -> str | None:
    """The address the Cloudflare Worker read from `cf-connecting-ip` (ADR-0025, AC12), or None.
    Trusted only when `OriginGuard.screen` matched the Worker's secret on this request (it strips
    the header otherwise), and only as exactly one header holding one bare IP address: no port,
    list or zone id. Behind the Worker, X-Forwarded-For carries Cloudflare's address, so without
    this every user would share one bucket; `HOPS` stays as it is for direct run.app callers."""
    if scope.get(SCOPE_KEY) is not True:
        return None
    values = [v for k, v in scope.get("headers") or () if k.lower() == CLIENT_IP_HEADER]
    if len(values) != 1:
        return None
    raw = values[0].decode("latin-1").strip()
    if not raw.isascii() or "%" in raw:
        return None
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return None
    return str(address)[:MAX_IP_CHARS]


def request_ip(request: Request) -> str:
    edge = edge_client_ip(request.scope)
    if edge is not None:
        return edge
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


def login_email_bucket(email: str) -> str:
    return f"login-email:{hashlib.sha256(email.strip().lower().encode()).hexdigest()}"


async def enforce_login_limit(request: Request, email: str) -> None:
    settings = get_settings()
    await _enforce(login_bucket(request_ip(request), email), settings.auth_login_rate_limit_per_min)
    await _enforce(login_email_bucket(email), settings.auth_login_email_rate_limit_per_min)


# V1 hardening #15: through the dashboard's `/api` proxy every browser can share one client IP
# (the proxy's egress), so the Google steps count per browser flow, with a much higher ceiling
# per IP as the backstop against one source rotating its flow ids.
GOOGLE_IP_CEILING: Final = 20


def google_bucket(ip: str, flow: str = "") -> str:
    return f"google:{hashlib.sha256(f'{ip}|{flow}'.encode()).hexdigest()}"


def google_ip_bucket(ip: str) -> str:
    return f"google-ip:{hashlib.sha256(ip.encode()).hexdigest()}"


def google_flow(binding_secret: str) -> str:
    """The flow key of a step that carries the browser's binding secret: the same value `start`
    got as `b` (base64url of its sha256), so one browser's steps share one bucket."""
    digest = hashlib.sha256(binding_secret.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def enforce_google_limit(request: Request, flow: str | None = None) -> None:
    """ADR-0020: every Google sign-in step, before any handle is read: per `sha256(ip|flow)`
    at the configured rate (flow: `b` at start, the `state` at the callback, the binding's `b`
    afterwards), and per `sha256(ip)` at `GOOGLE_IP_CEILING` times that rate."""
    limit = get_settings().auth_google_rate_limit_per_min
    ip = request_ip(request)
    await _enforce(google_ip_bucket(ip), limit * GOOGLE_IP_CEILING)
    await _enforce(google_bucket(ip, flow or ""), limit)


def signup_bucket(ip: str) -> str:
    return f"signup:{hashlib.sha256(ip.encode()).hexdigest()}"


async def enforce_signup_limit(request: Request) -> None:
    await _enforce(signup_bucket(request_ip(request)), get_settings().auth_login_rate_limit_per_min)


# ADR-0019 amendment (11 Nov 2026): the waiting page polls every 15 s (4 a minute), so one handle
# gets twice the login rate (room for a refresh when the tab comes back), and one address gets
# `SIGNUP_POLL_IP_CEILING` times that across handles (browsers behind one proxy egress).
SIGNUP_POLL_PER_HANDLE: Final = 2
SIGNUP_POLL_IP_CEILING: Final = 20


def signup_poll_bucket(ip: str, handle: str) -> str:
    return f"signup-poll:{hashlib.sha256(f'{ip}|{handle}'.encode()).hexdigest()}"


def signup_poll_ip_bucket(ip: str) -> str:
    return f"signup-poll-ip:{hashlib.sha256(ip.encode()).hexdigest()}"


async def enforce_signup_poll_limit(request: Request, handle: str) -> None:
    """The status poll, before the handle is looked up: per `sha256(ip|handle)` and per
    `sha256(ip)`. The handle itself never appears in a bucket key."""
    limit = get_settings().auth_login_rate_limit_per_min * SIGNUP_POLL_PER_HANDLE
    ip = request_ip(request)
    await _enforce(signup_poll_ip_bucket(ip), limit * SIGNUP_POLL_IP_CEILING)
    await _enforce(signup_poll_bucket(ip, handle), limit)


def invite_bucket(ip: str) -> str:
    return f"invite:{hashlib.sha256(ip.encode()).hexdigest()}"


async def enforce_invite_limit(request: Request) -> None:
    """SI5 (ADR-0029): the invite lookup, accept and join steps share one per-IP bucket, checked
    before the token is read."""
    await _enforce(invite_bucket(request_ip(request)), get_settings().auth_login_rate_limit_per_min)


def reset_bucket(ip: str) -> str:
    return f"reset:{hashlib.sha256(ip.encode()).hexdigest()}"


async def enforce_reset_limit(request: Request) -> None:
    """SI5 (ADR-0029): the reset-link redeem, per IP, checked before the token is read."""
    await _enforce(reset_bucket(request_ip(request)), get_settings().auth_login_rate_limit_per_min)


async def enforce_tenant_limit(tenant_id: UUID) -> None:
    await _enforce(f"t:{tenant_id}", get_settings().rate_limit_per_tenant_per_min)


async def enforce_refused_principal_limit(tenant_id: UUID, user_id: UUID) -> None:
    await _enforce(f"u:{tenant_id}:{user_id}", get_settings().rate_limit_per_tenant_per_min)
