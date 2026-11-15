"""Download one photo link a seller put in a spreadsheet (G2): the only code that opens it.

The link is user-supplied, so every request is treated as SSRF-critical:

- `check_url`: `https` only, port 443 only, no `user:password@`, an ASCII path and query, a host
  name (never an IP literal), no trailing dot, and not a name on the deny list (`localhost`,
  `*.localhost`, `*.local`, `*.internal`, `metadata`, `metadata.google.internal`).
- The host is resolved with `getaddrinfo` under a short timeout; every address it answers must
  pass `is_public_address` (`app.core.public_address`), else `blocked_address`.
- The connection is pinned to the vetted address: the request URL names the IP, the `Host` header
  and the TLS SNI name the host (`sni_hostname`), so the certificate is still checked for the host
  name and nothing resolves the name again after the check (no DNS-rebinding window).
- A fresh client per hop: `trust_env=False` (no proxy or netrc from the environment), no redirect
  following, no cookie jar carried between hops, TLS verified. The headers are exactly
  `USER_AGENT` (a constant, no user data), `Accept: image/jpeg,image/png` and
  `Accept-Encoding: identity`; no Authorization, Cookie or Referer is ever sent.
- Redirects are followed by hand, at most `max_redirects` (3) hops; every hop goes through the same
  check, resolve and pin. A relative `Location` is joined to the current link. An `http` hop is
  `unreachable`, an IP-literal or private hop `blocked_address`, a fourth hop
  `too_many_redirects`.
- Timeouts: connect 5 s, read 10 s, write 5 s, pool 5 s, and 30 s for the whole link (a slow drip
  that keeps each read under 10 s still ends).
- Only a 200 is read. 404/410 `not_found`, 401/403 `not_public`, 429 and 5xx `server_error`, any
  other status `unreachable`.
- Size: a `Content-Length` over `max_bytes` (the upload limit, 10 MiB) is refused unread; the body
  is streamed decoded (`aiter_bytes`, so a gzip body is capped too) and the read stops past the cap.
- Content: a `text/html` or `image/svg+xml` type is `not_image` unread; then the magic bytes
  decide (`app.services.image_header`): JPEG and PNG pass; GIF, WebP, TIFF, HEIC and BMP are
  `unsupported_type`; anything else is `not_image`. A Google Drive or Dropbox link that answers a
  web page is `not_public` (the file is not shared publicly). The header's size must be non-zero
  (else `corrupt`) and within `MAX_PIXELS` and `MAX_SIDE` (else `too_many_pixels`). The full decode
  stays in the isolated image check (CP6), exactly as for an upload.

Nothing about the link (its host, path or query) and no httpx exception text is ever logged or put
in a result: a result carries a closed `problem` and the HTTP status only. The resolver and the
transport are injectable for tests. `local_hosts` (`SHEET_PHOTOS_LOCAL_HOSTS`, refused by the
settings unless `APP_ENV=local`) names `host:port` pairs fetched over plain http with no address
check, for the local e2e fixture server only.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

import httpx

from app.core.public_address import is_public_address
from app.services.image_checks import MAX_PIXELS, MAX_SIDE
from app.services.image_header import ACCEPTED, MIME, dimensions, sniff
from app.services.images import MAX_IMAGE_BYTES

__all__ = [
    "HEADERS",
    "USER_AGENT",
    "FetchFailure",
    "FetchOk",
    "Limits",
    "PhotoFetcher",
    "Problem",
    "Resolver",
    "check_url",
]

Problem = Literal[
    "not_found",
    "not_public",
    "not_image",
    "unsupported_type",
    "too_large",
    "too_many_pixels",
    "corrupt",
    "timeout",
    "unreachable",
    "blocked_address",
    "too_many_redirects",
    "server_error",
]
Resolver = Callable[[str], Awaitable[Sequence[str]]]

USER_AGENT: Final = "Mozilla/5.0 (compatible; ProductPhotoFetcher/1.0)"
HEADERS: Final = {
    "user-agent": USER_AGENT,
    "accept": "image/jpeg,image/png",
    "accept-encoding": "identity",
}
_REDIRECTS: Final = frozenset({301, 302, 303, 307, 308})
_DENY_NAMES: Final = frozenset({"localhost", "metadata", "metadata.google.internal"})
_DENY_SUFFIXES: Final = (".localhost", ".local", ".internal")
# hosts whose web page for a shared file means "not shared publicly"
_SHARE_HOSTS: Final = frozenset(
    {"drive.google.com", "docs.google.com", "drive.usercontent.google.com", "www.dropbox.com",
     "dropbox.com", "dl.dropboxusercontent.com"}
)  # fmt: skip
_PAGE_TYPES: Final = ("text/html", "image/svg+xml", "application/xhtml")
_UNSUPPORTED: Final = frozenset({"gif", "webp", "tiff", "heic", "bmp"})


@dataclass(frozen=True, slots=True)
class Limits:
    connect_s: float = 5.0
    read_s: float = 10.0
    write_s: float = 5.0
    pool_s: float = 5.0
    total_s: float = 30.0
    resolve_s: float = 3.0
    max_bytes: int = MAX_IMAGE_BYTES
    max_redirects: int = 3


@dataclass(frozen=True, slots=True)
class FetchOk:
    data: bytes
    mime: str
    width: int
    height: int

    def __repr__(self) -> str:  # never the bytes
        return f"FetchOk({self.mime}, {self.width}x{self.height}, {len(self.data)} bytes)"


@dataclass(frozen=True, slots=True)
class FetchFailure:
    problem: Problem
    http_status: int | None = None
    # `unsupported_type`: which format it was (`webp`, `gif`, ...), for the seller message
    kind: str | None = None


@dataclass(frozen=True, slots=True)
class _Target:
    host: str
    port: int | None  # None: the scheme's default
    raw_path: bytes
    local: bool = False

    @property
    def url(self) -> httpx.URL:
        """The logical link (by host name): what a relative redirect is joined to."""
        scheme = "http" if self.local else "https"
        return httpx.URL(scheme=scheme, host=self.host, port=self.port, raw_path=self.raw_path)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _denied_name(host: str) -> bool:
    return host in _DENY_NAMES or host.endswith(_DENY_SUFFIXES)


def check_url(url: str, local_hosts: Collection[str] = ()) -> _Target | FetchFailure:
    """The link's host, port and raw path, or why it is never opened (module docstring)."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return FetchFailure("unreachable")
    if not url.isascii() or any(c in url for c in "\r\n\t\x00 "):
        return FetchFailure("unreachable")
    path = parts.path or "/"
    raw = (path + (f"?{parts.query}" if parts.query else "")).encode("ascii")
    if parts.scheme in ("http", "https") and host and f"{host}:{port}" in local_hosts:
        return _Target(host, port, raw, local=True)
    if parts.scheme != "https" or "@" in parts.netloc or not host:
        return FetchFailure("unreachable")
    if _is_ip_literal(host):
        return FetchFailure("blocked_address")
    if port not in (None, 443):
        return FetchFailure("unreachable")
    if host.endswith(".") or _denied_name(host):
        return FetchFailure("blocked_address")
    return _Target(host, None, raw)


async def default_resolver(host: str) -> Sequence[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _status_problem(status: int) -> Problem:
    if status in (404, 410):
        return "not_found"
    if status in (401, 403):
        return "not_public"
    if status == 429 or status >= 500:
        return "server_error"
    return "unreachable"


class PhotoFetcher:
    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        limits: Limits | None = None,
        local_hosts: Collection[str] = (),
    ) -> None:
        self._resolver = resolver or default_resolver
        self._transport = transport
        self._limits = limits or Limits()
        self._local = frozenset(h.strip().lower() for h in local_hosts if h.strip())

    async def fetch(self, url: str) -> FetchOk | FetchFailure:
        try:
            async with asyncio.timeout(self._limits.total_s):
                return await self._follow(url)
        except TimeoutError:
            return FetchFailure("timeout")

    async def _follow(self, url: str) -> FetchOk | FetchFailure:
        target = check_url(url, self._local)
        if isinstance(target, FetchFailure):
            return target
        share = target.host in _SHARE_HOSTS
        for _hop in range(self._limits.max_redirects + 1):
            answer = await self._get(target, share=share)
            if not isinstance(answer, httpx.URL):
                return answer
            target = check_url(str(answer), self._local)
            if isinstance(target, FetchFailure):
                return target
            share = share or target.host in _SHARE_HOSTS
        return FetchFailure("too_many_redirects")

    async def _address(self, host: str) -> str | FetchFailure:
        try:
            async with asyncio.timeout(self._limits.resolve_s):
                found = list(await self._resolver(host))
        except TimeoutError:
            return FetchFailure("timeout")
        except (OSError, UnicodeError, ValueError):
            return FetchFailure("unreachable")
        if not found:
            return FetchFailure("unreachable")
        if not all(is_public_address(a) for a in found):
            return FetchFailure("blocked_address")
        ip = ipaddress.ip_address(found[0].split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return str(ip)

    def _client(self) -> httpx.AsyncClient:
        limits = self._limits
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(
                connect=limits.connect_s,
                read=limits.read_s,
                write=limits.write_s,
                pool=limits.pool_s,
            ),
            follow_redirects=False,
            trust_env=False,
            verify=True,
            headers=HEADERS,
        )

    async def _get(self, target: _Target, *, share: bool) -> FetchOk | FetchFailure | httpx.URL:
        """One hop: the image, a failure, or the next link to check (a redirect)."""
        if target.local:
            url = target.url
            extensions: dict[str, str] = {}
        else:
            address = await self._address(target.host)
            if isinstance(address, FetchFailure):
                return address
            url = httpx.URL(scheme="https", host=address, raw_path=target.raw_path)
            extensions = {"sni_hostname": target.host}
        host_header = target.host if target.port is None else f"{target.host}:{target.port}"
        # Every failure is a value set inside the `except`, returned after it: no httpx
        # exception (its text can hold the link) ever becomes a result's context.
        failure: FetchFailure | None = None
        async with self._client() as client:
            request = client.build_request(
                "GET", url, headers={"host": host_header}, extensions=extensions
            )
            try:
                response = await client.send(request, stream=True)
            except httpx.TimeoutException:
                failure = FetchFailure("timeout")
            except httpx.HTTPError:
                failure = FetchFailure("unreachable")
            if failure is not None:
                return failure
            try:
                return await self._read(response, target, share=share)
            except httpx.TimeoutException:
                failure = FetchFailure("timeout")
            except httpx.HTTPError:
                failure = FetchFailure("unreachable")
            finally:
                await response.aclose()
        return failure

    async def _read(
        self, response: httpx.Response, target: _Target, *, share: bool
    ) -> FetchOk | FetchFailure | httpx.URL:
        status = response.status_code
        if status in _REDIRECTS:
            location = response.headers.get("location")
            if not location:
                return FetchFailure("unreachable", status)
            try:
                return target.url.join(location)
            except (httpx.InvalidURL, ValueError):
                return FetchFailure("unreachable", status)
        if status != 200:
            return FetchFailure(_status_problem(status), status)
        content_type = response.headers.get("content-type", "").lower()
        if any(t in content_type for t in _PAGE_TYPES):
            return FetchFailure("not_public" if share else "not_image", status)
        declared = (response.headers.get("content-length") or "").strip()
        if declared.isdigit() and int(declared) > self._limits.max_bytes:
            return FetchFailure("too_large", status)
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self._limits.max_bytes:
                return FetchFailure("too_large", status)
            chunks.append(chunk)
        return _judge(b"".join(chunks), status, share=share)


def _judge(data: bytes, status: int, *, share: bool) -> FetchOk | FetchFailure:
    kind = sniff(data)
    if kind in _UNSUPPORTED:
        return FetchFailure("unsupported_type", status, kind)
    if kind not in ACCEPTED:
        page = kind in ("html", "svg")
        return FetchFailure("not_public" if share and page else "not_image", status)
    size = dimensions(data, kind)
    if size is None or size[0] <= 0 or size[1] <= 0:
        return FetchFailure("corrupt", status)
    width, height = size
    if width * height > MAX_PIXELS or max(width, height) > MAX_SIDE:
        return FetchFailure("too_many_pixels", status)
    return FetchOk(data, MIME[kind], width, height)
