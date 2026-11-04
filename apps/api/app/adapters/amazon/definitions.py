"""Amazon Product Type Definitions 2020-09-01 (DW2 T4; T53): fetch one definition and its schema.

Used only by the staff command `app.cli templates refresh`, under the platform-owned SP-API
authorization (`PlatformTokenSource`, never a tenant token).

- The definition is one `SpApiClient.get_json("ptd_get", ...)`: the client's limiter, token
  refresh, 429 budget, redirect refusal, body cap and error map all apply.
- The envelope must name the asked product type, a `GET` schema link, a checksum, a version and
  an enforcement value; anything else is `MarketplaceUnavailable`.
- The schema link is checked before any request: `https`, no userinfo, port 443 only, a host on
  `AMAZON_PTD_LINK_HOSTS` (`.x.y` is a suffix), never an IP literal or a trailing dot. The host
  is resolved once and every address must be public (no private, loopback, link-local incl. the
  metadata address, CGNAT, unique-local, reserved, multicast or unspecified, and a v4-mapped v6
  address is judged by its v4 address). The download then connects to the checked address with
  `Host` and the TLS server name set to the hostname, so DNS rebinding cannot move it and the
  certificate is still verified for the hostname (C7).
- The download sends no token (the link is pre-signed), follows no redirect, ignores the
  environment and reads at most `SCHEMA_MAX_BODY` decoded bytes. A non-200, timeout or oversize
  answer is `MarketplaceUnavailable`.
- The body's base64(md5) must equal the envelope checksum, and it must parse (no NaN, no deep
  nesting) to a JSON object; else `SchemaInvalid` (`SCHEMA_INVALID`).
- The digest is sha256 of the canonical JSON (sorted keys, compact, UTF-8), so a re-serialized
  schema has the same digest.
- One log line `marketplace.call` for the download, with the fixed path `<schema link>`: never
  the link, its host or its signature. Every error is raised `from None`, so no httpx exception
  (which holds the pre-signed URL) is reachable from it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import socket
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, NamedTuple
from urllib.parse import SplitResult, urlsplit

import httpx
from mkos_schemas.codes import ErrorCode
from pydantic import SecretStr

from app.adapters.amazon import constants as c
from app.adapters.amazon.client import TIMEOUT, SpApiClient, failure
from app.adapters.base import AdapterError, AuthExpired, MarketplaceUnavailable
from app.adapters.oauth.base import OAuthError, OAuthRejectedError
from app.core.logging import get_logger

log = get_logger(__name__)

ENFORCEMENT: Final = ("ENFORCED", "NOT_ENFORCED")
AMAZON_SCHEMA_HOST: Final = "schemas.amazon.com"
LOG_PATH: Final = "<schema link>"
TOKEN_MARGIN: Final = timedelta(seconds=60)
MAX_DISPLAY_NAME: Final = 256
MAX_META_SCHEMA_ID: Final = 512
_CGNAT: Final = ipaddress.ip_network("100.64.0.0/10")
_ULA: Final = ipaddress.ip_network("fc00::/7")


class SchemaInvalid(AdapterError):  # noqa: N818 - the plan's name (DW2 T4)
    """The schema failed its checksum or is not a JSON object."""


def _unavailable(message: str) -> MarketplaceUnavailable:
    return MarketplaceUnavailable(failure(ErrorCode.MARKETPLACE_UNAVAILABLE, message))


def _invalid(message: str) -> SchemaInvalid:
    return SchemaInvalid(failure(ErrorCode.SCHEMA_INVALID, message))


def canonical_digest(schema: Mapping[str, Any]) -> bytes:
    """sha256 of the canonical JSON: sorted keys, compact separators, UTF-8."""
    text = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).digest()


@dataclass(frozen=True)
class FetchedDefinition:
    marketplace_id: str
    product_type: str
    requirements: str
    locale: str
    product_type_version: str
    requirements_enforced: str
    display_name: str | None
    property_groups: Mapping[str, Any]
    meta_schema_id: str | None
    schema: Mapping[str, Any]
    digest: bytes

    def __repr__(self) -> str:
        return f"FetchedDefinition({self.product_type} {self.product_type_version})"


class LinkPolicy(NamedTuple):
    """The hosts a schema link may name: `.x.y` matches any subdomain of x.y, else exact."""

    hosts: tuple[str, ...]

    @classmethod
    def from_setting(cls, value: str) -> LinkPolicy:
        return cls(tuple(part.strip().lower() for part in value.split(",") if part.strip()))

    def allows(self, host: str) -> bool:
        for entry in self.hosts:
            if entry.startswith("."):
                if host.endswith(entry) and len(host) > len(entry):
                    return True
            elif host == entry:
                return True
        return False


async def _resolve(host: str) -> Sequence[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def is_public_address(address: str) -> bool:
    """True only for a global unicast address (C7)."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return False
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip in _ULA:
        return False
    return ip.is_global


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class _Link:
    host: str
    raw_path: bytes


def check_link(resource: str, policy: LinkPolicy) -> _Link:
    """The link's host and raw path, or `MarketplaceUnavailable` when it is off policy."""
    refused = _unavailable("Amazon named a schema link this service does not fetch.")
    parts, port = _split(resource)
    if parts is None:
        raise refused
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or "@" in parts.netloc
        or port not in (None, 443)
        or not host
        or host.endswith(".")
        or _is_ip_literal(host)
        or not policy.allows(host)
    ):
        raise refused
    path = parts.path or "/"
    raw = path + (f"?{parts.query}" if parts.query else "")
    if not raw.isascii():
        raise refused
    return _Link(host, raw.encode("ascii"))


def _split(resource: str) -> tuple[SplitResult | None, int | None]:
    # Errors are turned into a value here, so the raise in `check_link` carries no context that
    # could hold the pre-signed link.
    try:
        parts = urlsplit(resource)
        return parts, parts.port
    except ValueError:
        return None, None


def _meta_schema_id(envelope: Mapping[str, Any], schema: Mapping[str, Any]) -> str | None:
    meta = envelope.get("metaSchema")
    link = meta.get("link") if isinstance(meta, dict) else None
    resource = link.get("resource") if isinstance(link, dict) else None
    candidate: str | None = None
    if isinstance(resource, str):
        parts = urlsplit(resource)
        if parts.scheme == "https" and (parts.hostname or "").lower() == AMAZON_SCHEMA_HOST:
            candidate = f"https://{AMAZON_SCHEMA_HOST}{parts.path}"
    if candidate is None and isinstance(schema.get("$schema"), str):
        parts = urlsplit(schema["$schema"])
        candidate = f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme else None
    if (
        candidate is None
        or len(candidate) > MAX_META_SCHEMA_ID
        or any(ch in candidate for ch in "?#")
    ):
        return None
    return candidate


def _refuse_constant(_value: str) -> Any:
    raise ValueError("NaN and Infinity are not JSON")


_UNREADABLE: Final = object()


def _loads(body: bytes) -> Any:
    try:
        return json.loads(body, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):  # a deeply nested body is unreadable too
        return _UNREADABLE


def parse_schema(body: bytes) -> dict[str, Any]:
    # `_loads` returns a sentinel instead of raising, so the SchemaInvalid below has no context.
    parsed = _loads(body)
    if parsed is _UNREADABLE:
        raise _invalid("The product type schema is not valid JSON.")
    if not isinstance(parsed, dict):
        raise _invalid("The product type schema is not a JSON object.")
    return parsed


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


class DefinitionsClient:
    def __init__(
        self,
        sp: SpApiClient,
        *,
        marketplace_id: str,
        link_policy: LinkPolicy,
        transport: httpx.AsyncBaseTransport | None = None,
        max_schema: int = c.SCHEMA_MAX_BODY,
        resolver: Callable[[str], Awaitable[Sequence[str]]] = _resolve,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sp = sp
        self._marketplace_id = marketplace_id
        self._policy = link_policy
        self._max_schema = max_schema
        self._resolver = resolver
        self._clock = clock
        self._http = httpx.AsyncClient(
            transport=transport, timeout=TIMEOUT, follow_redirects=False, trust_env=False
        )

    def __repr__(self) -> str:
        return "DefinitionsClient()"

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._sp.aclose()

    async def get_product_type(
        self,
        product_type: str,
        *,
        requirements: str = c.PTD_REQUIREMENTS,
        locale: str = c.PTD_LOCALE,
    ) -> FetchedDefinition:
        if requirements not in c.PTD_REQUIREMENTS_VALUES:
            raise ValueError("unknown requirements value")
        reply = await self._sp.get_json(
            "ptd_get",
            product_type=product_type,
            query={
                "marketplaceIds": self._marketplace_id,
                "requirements": requirements,
                "locale": locale,
            },
        )
        envelope = reply.json
        unexpected = _unavailable("Amazon answered with an unexpected definition.")
        if reply.status != 200 or not isinstance(envelope, dict):
            raise unexpected
        schema_ref = envelope.get("schema")
        link = schema_ref.get("link") if isinstance(schema_ref, dict) else None
        version = envelope.get("productTypeVersion")
        resource = link.get("resource") if isinstance(link, dict) else None
        checksum = schema_ref.get("checksum") if isinstance(schema_ref, dict) else None
        version_id = version.get("version") if isinstance(version, dict) else None
        enforced = envelope.get("requirementsEnforced")
        if (
            envelope.get("productType") != product_type
            or not isinstance(resource, str)
            or not isinstance(link, dict)
            or link.get("verb") != "GET"
            or not isinstance(checksum, str)
            or not isinstance(version_id, str)
            or not version_id
            or len(version_id) > 128
            or enforced not in ENFORCEMENT
        ):
            raise unexpected
        body = await self._download(check_link(resource, self._policy))
        expected = base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode()
        if expected != checksum:
            raise _invalid("The product type schema failed its checksum.")
        schema = parse_schema(body)
        groups = envelope.get("propertyGroups")
        display = _str(envelope.get("displayName"))
        return FetchedDefinition(
            marketplace_id=self._marketplace_id,
            product_type=product_type,
            requirements=requirements,
            locale=locale,
            product_type_version=version_id,
            requirements_enforced=str(enforced),
            display_name=display[:MAX_DISPLAY_NAME] if display else None,
            property_groups=groups if isinstance(groups, dict) else {},
            meta_schema_id=_meta_schema_id(envelope, schema),
            schema=schema,
            digest=canonical_digest(schema),
        )

    async def _resolve_quietly(self, host: str) -> list[str]:
        try:
            return list(await self._resolver(host))
        except (OSError, UnicodeError):  # an unresolvable host is refused below
            return []

    async def _addresses(self, host: str) -> list[str]:
        found = await self._resolve_quietly(host)
        if not found or not all(is_public_address(a) for a in found):
            raise _unavailable("The schema link's host is not a public address.")
        return found

    async def _download(self, link: _Link) -> bytes:
        address = (await self._addresses(link.host))[0]
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        url = httpx.URL(scheme="https", host=str(ip), raw_path=link.raw_path)
        request = self._http.build_request(
            "GET",
            url,
            headers={"host": link.host, "accept": "application/json"},
            extensions={"sni_hostname": link.host},
        )
        started = self._clock()
        # Every failure is recorded as a message and raised after the `except` block, so no
        # httpx exception (it holds the pre-signed URL) is the error's __context__.
        problem: str | None = None
        error: str | None = None
        status: int | None = None
        size = 0
        chunks: list[bytes] = []
        try:
            response = await self._http.send(request, stream=True)
        except httpx.HTTPError as exc:
            problem, error = "The product type schema could not be downloaded.", type(exc).__name__
        else:
            try:
                status = response.status_code
                if status != 200:
                    problem = "The product type schema link was refused."
                else:
                    async for chunk in response.aiter_bytes():  # decoded: the cap holds for gzip
                        size += len(chunk)
                        if size > self._max_schema:
                            problem = "The product type schema is too large."
                            break
                        chunks.append(chunk)
            except httpx.HTTPError as exc:
                problem, error = "The product type schema download was cut off.", type(exc).__name__
            finally:
                await response.aclose()
        extra = {"error": error} if error else {}
        self._log(started, status, size, **extra)
        if problem is not None:
            raise _unavailable(problem)
        return b"".join(chunks)

    def _log(self, started: float, status: int | None, size: int, **extra: Any) -> None:
        log.info(
            "marketplace.call",
            marketplace="amazon",
            operation="ptd_schema",
            method="GET",
            path=LOG_PATH,
            duration_ms=round((self._clock() - started) * 1000, 1),
            status=status,
            size=size,
            **extra,
        )


class PlatformTokenSource:
    """A `TokenSource` over the platform-owned LWA refresh token (T53): never a tenant token. The
    access token is cached until 60 s before it expires."""

    def __init__(
        self,
        oauth: Any,
        refresh_token: SecretStr,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._oauth = oauth
        self._refresh_token = refresh_token
        self._now = now
        self._token: SecretStr | None = None
        self._expires: datetime | None = None

    def __repr__(self) -> str:
        return "PlatformTokenSource()"

    async def get(self) -> SecretStr:
        if self._token is None or (
            self._expires is not None and self._now() >= self._expires - TOKEN_MARGIN
        ):
            return await self.refresh()
        return self._token

    async def refresh(self) -> SecretStr:
        try:
            grant = await self._oauth.refresh(self._refresh_token)
        except OAuthRejectedError:
            raise AuthExpired(
                failure(ErrorCode.AUTH_EXPIRED, "Amazon refused the platform authorization.")
            ) from None
        except OAuthError:
            raise _unavailable("The Amazon token refresh did not complete.") from None
        self._token = grant.tokens.access_token
        self._expires = grant.expires_at
        return self._token


class IntervalLimiter:
    """An `OutboundLimiter` for a one-shot staff command: `rates[operation]` calls per second per
    operation, in process (C1). The platform quota is never shared with a tenant's."""

    def __init__(
        self,
        rates: Mapping[str, int],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._rates = dict(rates)
        self._clock = clock
        self._sleep = sleep
        self._next: dict[str, float] = {}

    async def acquire(self, operation: str) -> float:
        rate = self._rates.get(operation)
        if rate is None or rate <= 0:
            raise ValueError("unknown operation")
        now = self._clock()
        start = max(now, self._next.get(operation, now))
        if start > now:
            await self._sleep(start - now)
        self._next[operation] = start + 1.0 / rate
        return start
