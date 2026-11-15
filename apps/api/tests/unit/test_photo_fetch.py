"""G2: the sheet photo fetcher's guards (`app.adapters.photo_fetch`), with an injected resolver
and an httpx MockTransport, so no test opens a socket."""

from __future__ import annotations

import asyncio
import io
import struct
import zlib
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

import httpx
import pytest
from app.adapters.photo_fetch import (
    HEADERS,
    USER_AGENT,
    FetchFailure,
    FetchOk,
    Limits,
    PhotoFetcher,
    check_url,
)
from app.core.public_address import is_public_address
from PIL import Image

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"
MIB10 = 10_485_760


def png(width: int = 32, height: int = 24) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(out, format="PNG")
    return out.getvalue()


def jpeg(width: int = 32, height: int = 24) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(out, format="JPEG")
    return out.getvalue()


def png_header(width: int, height: int, pad: int = 0) -> bytes:
    """A PNG signature and IHDR claiming `width` x `height` (no pixels: never decoded)."""
    body = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", 13) + b"IHDR" + body
    chunk += struct.pack(">I", zlib.crc32(b"IHDR" + body))
    data = b"\x89PNG\r\n\x1a\n" + chunk
    return data + b"\x00" * pad


def resolver(answers: Mapping[str, Sequence[str]]) -> Callable[[str], Any]:
    calls: list[str] = []

    async def resolve(host: str) -> Sequence[str]:
        calls.append(host)
        if host not in answers:
            raise OSError("no such host")
        return answers[host]

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def fetcher(
    handler: Callable[[httpx.Request], Any],
    answers: Mapping[str, Sequence[str]] | None = None,
    **limits: Any,
) -> PhotoFetcher:
    return PhotoFetcher(
        resolver=resolver(answers if answers is not None else {"cdn.example.com": [PUBLIC_V4]}),
        transport=httpx.MockTransport(handler),
        limits=Limits(**limits),
    )


def image_response(data: bytes, content_type: str = "image/png", **headers: str) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=data, headers={"content-type": content_type, **headers})

    return handler


async def fetch(f: PhotoFetcher, url: str = "https://cdn.example.com/p/1.png") -> Any:
    return await f.fetch(url)


# ---- addresses ----

BLOCKED = [
    "10.0.0.1",
    "172.16.0.1",
    "192.168.1.1",
    "127.0.0.1",
    "169.254.169.254",
    "100.64.0.1",
    "0.0.0.0",  # noqa: S104 - the unspecified address, refused
    "224.0.0.1",
    "240.0.0.1",
    "255.255.255.255",
    "::",
    "::1",
    "fe80::1",
    "fe80::1%eth0",
    "fd00:ec2::254",
    "fc00::1",
    "ff02::1",
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
    "::ffff:10.0.0.1",
    "64:ff9b::a9fe:a9fe",
    "64:ff9b:1::1",
    "2002:a9fe:a9fe::",
    "::7f00:1",
    "2001::1",  # Teredo
    "2001:db8::1",  # documentation
    "not-an-address",
]


@pytest.mark.parametrize("address", BLOCKED)
def test_blocked_addresses(address: str) -> None:
    assert not is_public_address(address)


@pytest.mark.parametrize("address", [PUBLIC_V4, PUBLIC_V6, "::ffff:93.184.216.34", "8.8.8.8"])
def test_public_addresses(address: str) -> None:
    assert is_public_address(address)


@pytest.mark.parametrize("address", BLOCKED[:-1])
async def test_a_host_resolving_to_a_blocked_address_is_refused(address: str) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, content=png())

    result = await fetch(fetcher(handler, {"cdn.example.com": [address]}))
    assert result == FetchFailure("blocked_address")
    assert sent == []


async def test_a_mixed_answer_is_refused() -> None:
    sent: list[httpx.Request] = []
    f = fetcher(lambda r: sent.append(r), {"cdn.example.com": [PUBLIC_V4, "10.0.0.7"]})
    assert await fetch(f) == FetchFailure("blocked_address")
    assert sent == []


async def test_a_dns_failure_is_unreachable() -> None:
    assert await fetch(fetcher(image_response(png()), {})) == FetchFailure("unreachable")


async def test_a_slow_resolver_times_out() -> None:
    async def slow(host: str) -> Sequence[str]:
        await asyncio.sleep(5)
        return [PUBLIC_V4]

    f = PhotoFetcher(
        resolver=slow,
        transport=httpx.MockTransport(image_response(png())),
        limits=Limits(resolve_s=0.05),
    )
    assert await fetch(f) == FetchFailure("timeout")


# ---- the link itself ----


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("http://cdn.example.com/p.png", "unreachable"),
        ("ftp://cdn.example.com/p.png", "unreachable"),
        ("file:///etc/passwd", "unreachable"),
        ("https://user:pw@cdn.example.com/p.png", "unreachable"),
        ("https://cdn.example.com:8443/p.png", "unreachable"),
        ("https://127.0.0.1/p.png", "blocked_address"),
        ("https://[::1]/p.png", "blocked_address"),
        ("https://169.254.169.254/latest/meta-data/", "blocked_address"),
        ("https://metadata.google.internal/computeMetadata/v1/", "blocked_address"),
        ("https://metadata/computeMetadata/v1/", "blocked_address"),
        ("https://localhost/p.png", "blocked_address"),
        ("https://shop.localhost/p.png", "blocked_address"),
        ("https://printer.local/p.png", "blocked_address"),
        ("https://db.corp.internal/p.png", "blocked_address"),
        ("https://cdn.example.com./p.png", "blocked_address"),
        ("https://cdn.example.com/pé.png", "unreachable"),
        ("https:///p.png", "unreachable"),
    ],
)
async def test_links_never_opened(url: str, problem: str) -> None:
    sent: list[httpx.Request] = []
    answers = {h: [PUBLIC_V4] for h in ("cdn.example.com", "localhost", "metadata")}
    f = fetcher(lambda r: sent.append(r), answers)
    assert await f.fetch(url) == FetchFailure(problem)  # type: ignore[arg-type]
    assert sent == []


def test_check_url_accepts_a_plain_https_link() -> None:
    target = check_url("https://CDN.example.com/a/b.png?x=1")
    assert not isinstance(target, FetchFailure)
    assert target.host == "cdn.example.com" and target.raw_path == b"/a/b.png?x=1"


def test_local_hosts_only_when_named() -> None:
    assert check_url("https://127.0.0.1:4120/p.png") == FetchFailure("blocked_address")
    target = check_url("https://127.0.0.1:4120/p.png", ["127.0.0.1:4120"])
    assert not isinstance(target, FetchFailure) and target.local


# ---- pinning ----


async def test_the_connection_is_pinned_to_the_vetted_address() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})

    result = await fetch(fetcher(handler))
    assert isinstance(result, FetchOk)
    (request,) = sent
    assert request.url.host == PUBLIC_V4
    assert request.url.scheme == "https"
    assert request.headers["host"] == "cdn.example.com"
    assert request.extensions["sni_hostname"] == "cdn.example.com"


async def test_an_ipv6_answer_is_pinned_too() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, content=png())

    assert isinstance(await fetch(fetcher(handler, {"cdn.example.com": [PUBLIC_V6]})), FetchOk)
    assert sent[0].url.host == PUBLIC_V6


async def test_a_rebinding_resolver_is_asked_once_per_hop() -> None:
    """The name is resolved once and the request names the vetted IP: a second, private answer
    (DNS rebinding) is never used for the connection."""
    answers = iter([[PUBLIC_V4], ["127.0.0.1"]])
    asked: list[str] = []

    async def rebinding(host: str) -> Sequence[str]:
        asked.append(host)
        return next(answers)

    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, content=png())

    f = PhotoFetcher(resolver=rebinding, transport=httpx.MockTransport(handler))
    assert isinstance(await fetch(f), FetchOk)
    assert asked == ["cdn.example.com"]
    assert [r.url.host for r in sent] == [PUBLIC_V4]


async def test_headers_are_exactly_the_fixed_set() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, content=png())

    await fetch(fetcher(handler))
    headers = {k.lower(): v for k, v in sent[0].headers.items()}
    assert headers["user-agent"] == USER_AGENT == HEADERS["user-agent"]
    assert headers["accept"] == "image/jpeg,image/png"
    assert headers["accept-encoding"] == "identity"
    for banned in ("authorization", "cookie", "referer", "proxy-authorization"):
        assert banned not in headers
    assert set(headers) <= {"host", "user-agent", "accept", "accept-encoding", "connection"}


# ---- redirects ----


def chain(hops: int, final: bytes, **first: str) -> Callable[[httpx.Request], httpx.Response]:
    """/0 -> /1 -> ... -> /hops, which answers `final`."""

    def handler(request: httpx.Request) -> httpx.Response:
        n = int(request.url.path.strip("/").split(".")[0] or 0)
        if n < hops:
            headers = {"location": f"/{n + 1}", **(first if n == 0 else {})}
            return httpx.Response(302, headers=headers)
        return httpx.Response(200, content=final)

    return handler


async def test_three_redirects_pass_and_a_fourth_is_refused() -> None:
    ok = await fetcher(chain(3, png())).fetch("https://cdn.example.com/0")
    assert isinstance(ok, FetchOk)
    refused = await fetcher(chain(4, png())).fetch("https://cdn.example.com/0")
    assert refused == FetchFailure("too_many_redirects")


async def test_a_cookie_set_on_one_hop_is_never_sent_on_the_next() -> None:
    sent: list[httpx.Request] = []
    inner = chain(1, png(), **{"set-cookie": "session=abc; Path=/"})

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return inner(request)

    assert isinstance(await fetcher(handler).fetch("https://cdn.example.com/0"), FetchOk)
    assert len(sent) == 2
    assert "cookie" not in {k.lower() for k in sent[1].headers}


@pytest.mark.parametrize(
    ("location", "answers", "problem"),
    [
        ("http://cdn.example.com/1", {}, "unreachable"),
        ("https://127.0.0.1/1", {}, "blocked_address"),
        ("https://[fd00:ec2::254]/1", {}, "blocked_address"),
        (
            "https://internal.example.net/1",
            {"internal.example.net": ["10.1.2.3"]},
            "blocked_address",
        ),
        ("https://metadata.google.internal/", {}, "blocked_address"),
        ("https://cdn.example.com:22/1", {}, "unreachable"),
    ],
)
async def test_each_redirect_hop_is_checked_again(
    location: str, answers: dict[str, list[str]], problem: str
) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.headers["host"] == "cdn.example.com" and request.url.path == "/0":
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, content=png())

    f = fetcher(handler, {"cdn.example.com": [PUBLIC_V4], **answers})
    assert await f.fetch("https://cdn.example.com/0") == FetchFailure(problem)  # type: ignore[arg-type]
    assert len(sent) == 1


async def test_a_relative_redirect_is_joined_to_the_current_link() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path == "/a/b.png":
            return httpx.Response(301, headers={"location": "../c/d.png?x=1"})
        return httpx.Response(200, content=png())

    assert isinstance(await fetcher(handler).fetch("https://cdn.example.com/a/b.png"), FetchOk)
    assert sent[1].url.raw_path == b"/c/d.png?x=1"
    assert sent[1].headers["host"] == "cdn.example.com"


async def test_a_redirect_to_another_host_resolves_that_host() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.headers["host"] == "cdn.example.com":
            return httpx.Response(302, headers={"location": "https://img.example.org/x.png"})
        return httpx.Response(200, content=png())

    answers = {"cdn.example.com": [PUBLIC_V4], "img.example.org": ["8.8.4.4"]}
    assert isinstance(await fetch(fetcher(handler, answers)), FetchOk)
    assert [r.url.host for r in sent] == [PUBLIC_V4, "8.8.4.4"]
    assert sent[1].extensions["sni_hostname"] == "img.example.org"


async def test_a_redirect_without_location_is_unreachable() -> None:
    f = fetcher(lambda r: httpx.Response(302))
    assert await fetch(f) == FetchFailure("unreachable", 302)


# ---- status codes ----


@pytest.mark.parametrize(
    ("status", "problem"),
    [
        (404, "not_found"),
        (410, "not_found"),
        (401, "not_public"),
        (403, "not_public"),
        (429, "server_error"),
        (500, "server_error"),
        (503, "server_error"),
        (400, "unreachable"),
        (418, "unreachable"),
        (204, "unreachable"),
        (206, "unreachable"),
    ],
)
async def test_status_mapping(status: int, problem: str) -> None:
    f = fetcher(lambda r: httpx.Response(status, content=png()))
    assert await fetch(f) == FetchFailure(problem, status)  # type: ignore[arg-type]


# ---- size ----


async def test_a_declared_length_over_the_cap_is_refused_unread() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-length": str(MIB10 + 1)}, stream=httpx.ByteStream(b"")
        )

    assert await fetch(fetcher(handler)) == FetchFailure("too_large", 200)


async def test_a_streamed_body_over_the_cap_stops_reading() -> None:
    pulled: list[int] = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield png_header(10, 10)
            for _ in range(1000):
                pulled.append(1)
                yield b"\x00" * 512

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Body())

    assert await fetch(fetcher(handler, max_bytes=4096)) == FetchFailure("too_large", 200)
    assert len(pulled) < 20


async def test_exactly_the_upload_limit_passes() -> None:
    header = png_header(100, 100)
    data = header + b"\x00" * (MIB10 - len(header))
    assert len(data) == MIB10
    result = await fetch(fetcher(image_response(data)))
    assert isinstance(result, FetchOk) and len(result.data) == MIB10


# ---- type ----


@pytest.mark.parametrize(
    ("data", "content_type", "problem"),
    [
        (
            b"<!doctype html><html><body>Sign in</body></html>",
            "text/html; charset=utf-8",
            "not_image",
        ),
        (b"<!doctype html><html></html>", "image/png", "not_image"),  # mislabelled
        (b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', "image/svg+xml", "not_image"),
        (
            b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>',
            "image/png",
            "not_image",
        ),
        (b"\xef\xbb\xbf  <svg/>", "application/octet-stream", "not_image"),
        (b"GIF89a" + b"\x00" * 20, "image/gif", "unsupported_type"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 20, "image/webp", "unsupported_type"),
        (b"II*\x00" + b"\x00" * 20, "image/tiff", "unsupported_type"),
        (b"MM\x00*" + b"\x00" * 20, "image/tiff", "unsupported_type"),
        (b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20, "image/heic", "unsupported_type"),
        (b"BM" + b"\x00" * 30, "image/bmp", "unsupported_type"),
        (b"%PDF-1.7 ...", "application/pdf", "not_image"),
        (b"", "image/png", "not_image"),
    ],
)
async def test_wrong_types_are_refused(data: bytes, content_type: str, problem: str) -> None:
    result = await fetch(fetcher(image_response(data, content_type)))
    assert isinstance(result, FetchFailure) and result.problem == problem


async def test_the_unsupported_format_is_named() -> None:
    result = await fetch(fetcher(image_response(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 9)))
    assert result == FetchFailure("unsupported_type", 200, "webp")


@pytest.mark.parametrize("maker", [png, jpeg])
async def test_jpeg_and_png_pass(maker: Callable[..., bytes]) -> None:
    data = maker(40, 30)
    result = await fetch(fetcher(image_response(data, "application/octet-stream")))
    assert isinstance(result, FetchOk)
    assert (result.width, result.height) == (40, 30)
    assert result.mime in ("image/png", "image/jpeg")
    assert "bytes" in repr(result) and str(data[:8]) not in repr(result)


async def test_a_header_over_the_pixel_cap_is_refused() -> None:
    result = await fetch(fetcher(image_response(png_header(9000, 9000))))
    assert result == FetchFailure("too_many_pixels", 200)
    thin = await fetch(fetcher(image_response(png_header(20_001, 10))))
    assert thin == FetchFailure("too_many_pixels", 200)


async def test_a_zero_size_header_is_corrupt() -> None:
    assert await fetch(fetcher(image_response(png_header(0, 10)))) == FetchFailure("corrupt", 200)
    cut = await fetch(fetcher(image_response(b"\xff\xd8\xff\xe0\x00")))
    assert cut == FetchFailure("corrupt", 200)


async def test_a_drive_page_is_not_public() -> None:
    page = b"<!doctype html><html>Google Drive - Sign in</html>"
    f = fetcher(image_response(page, "text/html"), {"drive.google.com": [PUBLIC_V4]})
    result = await f.fetch("https://drive.google.com/uc?export=download&id=abcdefghij12")
    assert result == FetchFailure("not_public", 200)


# ---- timeouts and transport errors ----


async def test_a_slow_drip_body_hits_the_total_timeout() -> None:
    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield png_header(10, 10)
            while True:
                await asyncio.sleep(0.05)
                yield b"\x00"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Drip())

    assert await fetch(fetcher(handler, total_s=0.3)) == FetchFailure("timeout")


async def test_a_connect_timeout_is_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out connecting to the link")

    assert await fetch(fetcher(handler)) == FetchFailure("timeout")


async def test_a_connect_error_is_unreachable_and_carries_no_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("https://cdn.example.com/secret?token=abc refused")

    result = await fetch(fetcher(handler))
    assert result == FetchFailure("unreachable")
    assert "token" not in repr(result)


# ---- encoding: a compressed body is never inflated ----


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "zstd", "gzip, identity"])
async def test_a_compressed_body_is_refused_unread(encoding: str) -> None:
    import gzip

    # a gzip bomb: 64 MiB of zeros in well under 100 KiB, a valid PNG header in front
    bomb = gzip.compress(png_header(10, 10) + b"\x00" * (64 * 1024 * 1024))
    assert len(bomb) < 100_000
    pulled: list[int] = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            pulled.append(1)
            yield bomb

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "image/png", "content-encoding": encoding}, stream=Body()
        )

    assert await fetch(fetcher(handler)) == FetchFailure("corrupt", 200)
    assert pulled == []


async def test_an_identity_encoding_passes() -> None:
    data = png()
    result = await fetch(fetcher(image_response(data, **{"content-encoding": "identity"})))
    assert isinstance(result, FetchOk) and result.data == data


# ---- every vetted address, in order ----


async def test_the_next_vetted_address_is_tried_when_one_will_not_connect() -> None:
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.host)
        if request.url.host == PUBLIC_V6:
            raise httpx.ConnectError("no route")
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})

    f = fetcher(handler, {"cdn.example.com": [PUBLIC_V6, PUBLIC_V4]})
    assert isinstance(await fetch(f), FetchOk)
    assert tried == [PUBLIC_V6, PUBLIC_V4]


async def test_a_connect_timeout_moves_on_to_the_next_address() -> None:
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.host)
        if request.url.host == PUBLIC_V4:
            raise httpx.ConnectTimeout("slow")
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})

    f = fetcher(handler, {"cdn.example.com": [PUBLIC_V4, "8.8.8.8"]})
    assert isinstance(await fetch(f), FetchOk)
    assert tried == [PUBLIC_V4, "8.8.8.8"]


async def test_every_address_failing_is_the_last_failure() -> None:
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.host)
        if request.url.host == PUBLIC_V4:
            raise httpx.ConnectTimeout("slow")
        raise httpx.ConnectError("refused")

    f = fetcher(handler, {"cdn.example.com": [PUBLIC_V4, "8.8.8.8", "8.8.8.8"]})
    assert await fetch(f) == FetchFailure("unreachable")
    assert tried == [PUBLIC_V4, "8.8.8.8"]  # a repeated answer is tried once


async def test_an_answer_that_is_not_a_connect_failure_ends_the_hop() -> None:
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.host)
        raise httpx.ReadTimeout("slow body")

    f = fetcher(handler, {"cdn.example.com": [PUBLIC_V4, "8.8.8.8"]})
    assert await fetch(f) == FetchFailure("timeout")
    assert tried == [PUBLIC_V4]


async def test_trying_addresses_stays_within_the_total_timeout() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        raise httpx.ConnectError("refused")

    f = PhotoFetcher(
        resolver=resolver({"cdn.example.com": [PUBLIC_V4, "8.8.8.8", "1.1.1.1", "9.9.9.9"]}),
        transport=httpx.MockTransport(slow),
        limits=Limits(total_s=0.3),
    )
    assert await fetch(f) == FetchFailure("timeout")
