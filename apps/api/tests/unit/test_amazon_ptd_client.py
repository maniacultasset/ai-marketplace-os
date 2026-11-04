"""DW2 T3 and T4 (AC1-AC4, AC7): the SP-API client's PTD route and the definitions client, over
the in-process fake and fault transports (no sockets, no DNS: the resolver is a stub)."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from app.adapters.amazon import constants as c
from app.adapters.amazon.definitions import (
    DefinitionsClient,
    FetchedDefinition,
    IntervalLimiter,
    LinkPolicy,
    PlatformTokenSource,
    SchemaInvalid,
    canonical_digest,
)
from app.adapters.base import (
    AuthExpired,
    MarketplaceUnavailable,
    PermissionDenied,
    RateLimited,
)
from mkos_schemas.codes import ErrorCode
from pydantic import SecretStr
from tests.adapters.amazon_support import CountingLimiter, FakeTokens, FixedRng, spapi_client
from tests.support.fake_marketplace import build_fake, ptd_fixture
from tests.support.fault_transport import FaultTransport

QUERY = {
    "marketplaceIds": "A21TJRUUN4KGV",
    "requirements": "LISTING_PRODUCT_ONLY",
    "locale": "en_IN",
}
PUBLIC = "52.95.150.10"
POLICY = LinkPolicy((".amazonaws.com",))


async def _control(app: Any, **body: Any) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fake.test"
    ) as client:
        await client.post("/__control", json=body)


class CountingTokens(FakeTokens):
    def __init__(self, app: Any) -> None:
        super().__init__(app)
        self.gets = 0

    async def get(self) -> Any:
        self.gets += 1
        return await super().get()


class Recording(httpx.AsyncBaseTransport):
    """Delegates to the fake and keeps every request (the pinned URL, headers, extensions)."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self.inner.handle_async_request(request)


class Canned(httpx.AsyncBaseTransport):
    """Answers every request with one canned response (the link download faults)."""

    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _resolver(*addresses: str) -> Any:
    async def resolve(host: str) -> Sequence[str]:
        return list(addresses)

    return resolve


def _definitions(
    app: Any,
    *,
    link_transport: httpx.AsyncBaseTransport | None = None,
    resolver: Any = None,
    max_schema: int = c.SCHEMA_MAX_BODY,
    **client_over: Any,
) -> DefinitionsClient:
    sp = spapi_client(app, seller_id=None, **client_over)
    return DefinitionsClient(
        sp,
        marketplace_id="A21TJRUUN4KGV",
        link_policy=POLICY,
        transport=link_transport or httpx.ASGITransport(app),
        resolver=resolver or _resolver(PUBLIC),
        max_schema=max_schema,
    )


def _calls(app: Any, kind: str) -> list[dict[str, Any]]:
    return [call for call in app.state.fake.calls if call.get("kind") == kind]


# ---- T3: the route ----


async def test_ptd_raw_path_is_exact() -> None:
    app = build_fake()
    client = spapi_client(app, seller_id=None)
    reply = await client.get_json("ptd_get", product_type="KURTA", query=QUERY)
    assert reply.status == 200
    (call,) = _calls(app, "ptd")
    assert f"{call['raw_path']}?{call['query']}" == (
        "/definitions/2020-09-01/productTypes/KURTA"
        "?marketplaceIds=A21TJRUUN4KGV&requirements=LISTING_PRODUCT_ONLY&locale=en_IN"
    )


@pytest.mark.parametrize(
    "product_type",
    ["kurta", "KURTA/..", "..", "%2E%2E", "KURTA?x=1", "KÜRTA", "A" * 65, "", " ", None, 3],
)
async def test_ptd_refused_product_types_touch_nothing(product_type: Any) -> None:
    app = build_fake()
    tokens = CountingTokens(app)
    limiter = CountingLimiter()
    client = spapi_client(app, seller_id=None, tokens=tokens, limiter=limiter)
    with pytest.raises(ValueError):
        await client.get_json("ptd_get", product_type=product_type, query=QUERY)
    assert limiter.calls == [] and tokens.gets == 0 and app.state.fake.calls == []


async def test_ptd_unknown_operation_refused() -> None:
    app = build_fake()
    client = spapi_client(app, seller_id=None)
    with pytest.raises(ValueError):
        operation: Any = "ptd_search"
        await client.get_json(operation, product_type="KURTA", query=QUERY)


async def test_query_values_are_percent_encoded_on_the_wire() -> None:
    app = build_fake()
    client = spapi_client(app, seller_id=None)
    await client.get_json("ptd_get", product_type="KURTA", query={**QUERY, "locale": "कुर्ता"})
    (call,) = _calls(app, "ptd")
    assert "locale=%E0%A4%95" in call["query"]


@pytest.mark.parametrize("method", ["put", "patch", "get"])
async def test_seller_less_client_refuses_item_calls(method: str) -> None:
    app = build_fake()
    tokens = CountingTokens(app)
    limiter = CountingLimiter()
    client = spapi_client(app, seller_id=None, tokens=tokens, limiter=limiter)
    call = getattr(client, method)
    args: tuple[Any, ...] = ("SKU-1",) if method == "get" else ("SKU-1", {"attributes": {}})
    with pytest.raises(ValueError, match="seller_id"):
        await call(*args)
    assert limiter.calls == [] and tokens.gets == 0


@pytest.mark.parametrize("seller", ["", ".", ".."])
def test_unsafe_seller_still_refused(seller: str) -> None:
    with pytest.raises(ValueError, match="seller_id"):
        spapi_client(build_fake(), seller_id=seller)


async def test_ptd_429_budget() -> None:
    app = build_fake()
    await _control(app, ptd={"status": [429, 429, 429, 429]})
    client = spapi_client(app, seller_id=None, rng=FixedRng(0.0))
    with pytest.raises(RateLimited):
        await client.get_json("ptd_get", product_type="KURTA", query=QUERY)
    assert len(client.sleeps) == 3 and len(_calls(app, "ptd")) == 4
    app = build_fake()
    await _control(app, ptd={"status": [429, 429, 429]})
    client = spapi_client(app, seller_id=None, rng=FixedRng(0.0))
    assert (await client.get_json("ptd_get", product_type="KURTA", query=QUERY)).status == 200
    assert len(client.sleeps) == 3


async def test_ptd_auth_refresh_once() -> None:
    app = build_fake()
    tokens = FakeTokens(app)
    await _control(app, ptd={"deny": True})
    client = spapi_client(app, seller_id=None, tokens=tokens)
    # 403 once, then 200 after the one refresh
    assert (await client.get_json("ptd_get", product_type="KURTA", query=QUERY)).status == 200
    assert tokens.refreshes == 1
    # the US marketplace: 403, refresh, 403 -> PermissionDenied with PTD wording
    tokens = FakeTokens(app)
    client = spapi_client(app, seller_id=None, tokens=tokens)
    with pytest.raises(PermissionDenied) as caught:
        await client.get_json(
            "ptd_get", product_type="KURTA", query={**QUERY, "marketplaceIds": "ATVPDKIKX0DER"}
        )
    assert tokens.refreshes == 1
    message = caught.value.report.errors[0].message
    assert "product type definitions" in message and "account" not in message


async def test_ptd_401_twice_auth_expired() -> None:
    app = build_fake()
    await _control(app, ptd={"status": [401, 401]})
    tokens = FakeTokens(app)
    client = spapi_client(app, seller_id=None, tokens=tokens)
    with pytest.raises(AuthExpired):
        await client.get_json("ptd_get", product_type="KURTA", query=QUERY)
    assert tokens.refreshes == 1


@pytest.mark.parametrize(
    "fault",
    [
        "500",
        "timeout",
        "connect",
    ],
)
async def test_ptd_get_failures_are_unavailable(fault: str) -> None:
    app = build_fake()
    transport: Any = None
    if fault == "500":
        await _control(app, ptd={"status": [500]})
    elif fault == "timeout":
        transport = FaultTransport(httpx.ASGITransport(app), before_send=httpx.ReadTimeout("t"))
    else:
        transport = FaultTransport(httpx.ASGITransport(app), before_send=httpx.ConnectError("c"))
    client = spapi_client(app, seller_id=None, transport=transport)
    with pytest.raises(MarketplaceUnavailable):
        await client.get_json("ptd_get", product_type="KURTA", query=QUERY)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>"),
        httpx.Response(200, content=b'{"x":"' + b"a" * (1024 * 1024) + b'"}'),
        httpx.Response(302, headers={"location": "https://elsewhere.example/"}),
    ],
)
async def test_ptd_bad_answers_are_unavailable(response: httpx.Response) -> None:
    app = build_fake()
    transport = Canned(response)
    client = spapi_client(app, seller_id=None, transport=transport)
    with pytest.raises(MarketplaceUnavailable):
        await client.get_json("ptd_get", product_type="KURTA", query=QUERY)
    assert len(transport.requests) == 1


async def test_ptd_logs_the_template_only(log_lines: Any) -> None:
    app = build_fake()
    capture = log_lines()
    tokens = FakeTokens(app)
    client = spapi_client(app, seller_id=None, tokens=tokens)
    await client.get_json("ptd_get", product_type="KURTA", query=QUERY)
    lines = [line for line in capture.lines() if line.get("message") == "marketplace.call"]
    assert [(line["operation"], line["path"]) for line in lines] == [
        ("ptd_get", "/definitions/2020-09-01/productTypes/{productType}")
    ]
    text = capture.text()
    for leak in ("KURTA?", "marketplaceIds=", tokens.token, "amzn1."):
        assert leak not in text


# ---- T4: the definitions client ----


async def test_fetch_happy_path() -> None:
    app = build_fake()
    link = Recording(httpx.ASGITransport(app))
    client = _definitions(app, link_transport=link)
    fetched = await client.get_product_type("KURTA")
    expected = json.loads(ptd_fixture("KURTA.schema.json"))
    assert fetched.schema == expected
    assert fetched.digest == canonical_digest(expected) and len(fetched.digest) == 32
    assert fetched.product_type_version == "UFAKE0001"
    assert fetched.requirements == "LISTING_PRODUCT_ONLY" and fetched.locale == "en_IN"
    assert fetched.requirements_enforced == "ENFORCED"
    assert fetched.display_name == "Kurta"
    assert fetched.meta_schema_id == (
        "https://schemas.amazon.com/selling-partners/definitions/product-types/meta-schema/v1"
    )
    assert "KURTA" in repr(fetched) and "$defs" not in repr(fetched)
    (request,) = link.requests
    assert "x-amz-access-token" not in request.headers
    assert "authorization" not in request.headers
    assert request.url.host == PUBLIC and request.url.scheme == "https"
    assert request.headers["host"] == "ptd-schemas.fake.amazonaws.com"
    assert request.extensions["sni_hostname"] == "ptd-schemas.fake.amazonaws.com"
    assert b"X-Amz-Signature=" in request.url.raw_path
    assert len(_calls(app, "ptd")) == 1 and len(_calls(app, "ptd_schema")) == 1


def test_digest_is_canonical() -> None:
    schema = json.loads(ptd_fixture("KURTA.schema.json"))
    shuffled = json.loads(json.dumps(dict(reversed(list(schema.items())))))
    assert canonical_digest(schema) == canonical_digest(shuffled)
    raw = ptd_fixture("KURTA.schema.json")
    assert canonical_digest(schema) != hashlib.sha256(raw).digest()


async def test_bad_checksum_is_schema_invalid() -> None:
    app = build_fake()
    await _control(app, ptd={"bad_checksum": True})
    with pytest.raises(SchemaInvalid) as caught:
        await _definitions(app).get_product_type("KURTA")
    assert caught.value.report.error_code is ErrorCode.SCHEMA_INVALID


def _envelope_with(app: Any, resource: str, **over: Any) -> httpx.AsyncBaseTransport:
    """A transport whose PTD answer names `resource` as the schema link."""

    class Rewrite(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            inner = httpx.ASGITransport(app)
            response = await inner.handle_async_request(request)
            await response.aread()
            body = json.loads(response.content)
            body["schema"]["link"]["resource"] = resource
            body.update(over)
            return httpx.Response(200, json=body)

    return Rewrite()


@pytest.mark.parametrize(
    "resource",
    [
        "http://ptd.amazonaws.com/s",
        "https://evil.example/s",
        "https://169.254.169.254/latest/meta-data",
        "https://user@ptd.amazonaws.com/s",
        "https://ptd.amazonaws.com:8443/s",
        "https://amazonaws.com.evil.io/s",
        "https://ptd.amazonaws.com./s",
        "https://amazonaws.com/s",
        "https://[::1]/s",
        "ftp://ptd.amazonaws.com/s",
        "https://ptd.amazonaws.com:abc/s",
    ],
)
async def test_off_policy_links_refused_before_any_request(resource: str) -> None:
    app = build_fake()
    link = Recording(httpx.ASGITransport(app))
    client = _definitions(app, link_transport=link, transport=_envelope_with(app, resource))
    with pytest.raises(MarketplaceUnavailable):
        await client.get_product_type("KURTA")
    assert link.requests == []


@pytest.mark.parametrize(
    "addresses",
    [
        ("10.0.0.1",),
        ("169.254.169.254",),
        ("127.0.0.1",),
        ("100.64.0.1",),
        ("fd00::1",),
        ("fe80::1",),
        ("::ffff:10.0.0.1",),
        ("0.0.0.0",),  # noqa: S104 - an address to refuse, not a bind
        ("224.0.0.1",),
        (),
        (PUBLIC, "10.0.0.1"),
    ],
)
async def test_private_resolved_addresses_refused(addresses: tuple[str, ...]) -> None:
    app = build_fake()
    link = Recording(httpx.ASGITransport(app))
    client = _definitions(app, link_transport=link, resolver=_resolver(*addresses))
    with pytest.raises(MarketplaceUnavailable):
        await client.get_product_type("KURTA")
    assert link.requests == []


async def test_resolution_failure_refused() -> None:
    app = build_fake()

    async def broken(host: str) -> Sequence[str]:
        raise OSError("no such host")

    link = Recording(httpx.ASGITransport(app))
    with pytest.raises(MarketplaceUnavailable) as caught:
        await _definitions(app, link_transport=link, resolver=broken).get_product_type("KURTA")
    assert link.requests == [] and caught.value.__context__ is None


@pytest.mark.parametrize(
    "over",
    [
        {"productType": "SHIRT"},
        {"requirementsEnforced": "MAYBE"},
        {"productTypeVersion": {}},
        {
            "schema": {
                "link": {"resource": "https://x.amazonaws.com/s", "verb": "POST"},
                "checksum": "x",
            }
        },
        {"schema": {"link": {"resource": "https://x.amazonaws.com/s", "verb": "GET"}}},
        {"schema": {"checksum": "x"}},
    ],
)
async def test_unexpected_envelopes_refused(over: dict[str, Any]) -> None:
    app = build_fake()

    class Patch(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            response = await httpx.ASGITransport(app).handle_async_request(request)
            await response.aread()
            body = json.loads(response.content)
            body.update(over)
            return httpx.Response(200, json=body)

    link = Recording(httpx.ASGITransport(app))
    with pytest.raises(MarketplaceUnavailable):
        await _definitions(app, transport=Patch(), link_transport=link).get_product_type("KURTA")
    assert link.requests == []


async def test_link_redirect_and_expiry_refused() -> None:
    app = build_fake()
    await _control(app, ptd={"redirect_link": True})
    link = Recording(httpx.ASGITransport(app))
    with pytest.raises(MarketplaceUnavailable):
        await _definitions(app, link_transport=link).get_product_type("KURTA")
    assert len(link.requests) == 1
    expired = Canned(httpx.Response(403, content=b"<Error>AccessDenied</Error>"))
    with pytest.raises(MarketplaceUnavailable):
        await _definitions(app, link_transport=expired).get_product_type("KURTA")


async def test_link_timeout_unavailable_without_context() -> None:
    app = build_fake()
    fault = Canned(httpx.ReadTimeout("https://ptd.amazonaws.com/s?X-Amz-Signature=abc"))
    with pytest.raises(MarketplaceUnavailable) as caught:
        await _definitions(app, link_transport=fault).get_product_type("KURTA")
    assert caught.value.__cause__ is None and caught.value.__context__ is None


async def test_oversize_schema_refused_and_cap_boundary() -> None:
    app = build_fake()
    await _control(app, ptd={"oversize": True})
    with pytest.raises(MarketplaceUnavailable):
        await _definitions(app).get_product_type("KURTA")
    # a small gzip wire size that decodes past the cap
    bomb = gzip.compress(b'"' + b"x" * (c.SCHEMA_MAX_BODY + 10) + b'"')
    gz = Canned(httpx.Response(200, content=bomb, headers={"content-encoding": "gzip"}))
    with pytest.raises(MarketplaceUnavailable):
        await _definitions(app, link_transport=gz).get_product_type("KURTA")
    # exactly the cap is accepted (a small cap keeps the test fast)
    body = ptd_fixture("KURTA.schema.json")
    fetched = await _definitions(app, max_schema=len(body)).get_product_type("KURTA")
    assert fetched.schema == json.loads(body)
    with pytest.raises(MarketplaceUnavailable):
        await _definitions(app, max_schema=len(body) - 1).get_product_type("KURTA")


def _served(body: bytes, app: Any) -> tuple[httpx.AsyncBaseTransport, httpx.AsyncBaseTransport]:
    """An envelope whose checksum matches `body`, and a link that serves it."""
    checksum = base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode()

    class Envelope(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            response = await httpx.ASGITransport(app).handle_async_request(request)
            await response.aread()
            data = json.loads(response.content)
            data["schema"]["checksum"] = checksum
            return httpx.Response(200, json=data)

    return Envelope(), Canned(httpx.Response(200, content=body))


@pytest.mark.parametrize(
    "body",
    [b"[]", b'"x"', b"NaN", b'{"a": NaN}', b"[" * 100_000 + b"]" * 100_000, b"{not json"],
)
async def test_non_object_schemas_invalid(body: bytes) -> None:
    app = build_fake()
    envelope, link = _served(body, app)
    with pytest.raises(SchemaInvalid) as caught:
        await _definitions(app, transport=envelope, link_transport=link).get_product_type("KURTA")
    assert caught.value.__context__ is None


async def test_platform_token_source_caches_and_refreshes() -> None:
    from app.adapters.oauth.base import OAuthRejectedError, OAuthTransientError
    from app.core.crypto import TokenPair

    now = datetime(2026, 11, 3, tzinfo=UTC)

    class OAuth:
        def __init__(self) -> None:
            self.calls = 0
            self.fail: type[Exception] | None = None

        async def refresh(self, refresh_token: SecretStr) -> Any:
            from app.adapters.oauth.base import TokenGrant

            self.calls += 1
            if self.fail is not None:
                raise self.fail
            assert refresh_token.get_secret_value() == "Atzr|platform"
            pair = TokenPair(SecretStr(f"Atza|access-{self.calls}"), refresh_token)
            return TokenGrant(pair, now + timedelta(hours=1), (), None, None)

    oauth = OAuth()
    clock = [now]
    source = PlatformTokenSource(oauth, SecretStr("Atzr|platform"), now=lambda: clock[0])
    first = await source.get()
    assert (await source.get()) is first and oauth.calls == 1
    await source.refresh()
    assert oauth.calls == 2
    clock[0] = now + timedelta(minutes=59, seconds=30)  # inside the 60 s margin
    await source.get()
    assert oauth.calls == 3
    assert "Atz" not in repr(source)
    oauth.fail = OAuthRejectedError
    with pytest.raises(AuthExpired):
        await source.refresh()
    oauth.fail = OAuthTransientError
    with pytest.raises(MarketplaceUnavailable):
        await source.refresh()


async def test_interval_limiter() -> None:
    now = [100.0]
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    limiter = IntervalLimiter({"ptd_get": 1, "other": 1}, clock=lambda: now[0], sleep=sleep)
    await limiter.acquire("ptd_get")
    await limiter.acquire("other")
    assert sleeps == []
    await limiter.acquire("ptd_get")
    assert sleeps == [pytest.approx(1.0)]
    with pytest.raises(ValueError):
        await limiter.acquire("put")


async def test_nothing_secret_in_logs_or_errors(log_lines: Any) -> None:
    capture = log_lines()
    failures: list[BaseException] = []
    app = build_fake()
    tokens = FakeTokens(app)
    await _definitions(app, tokens=tokens).get_product_type("KURTA")
    for switch in ("bad_checksum", "redirect_link", "oversize"):
        await _control(app, ptd={switch: True})
        try:
            await _definitions(app, tokens=tokens).get_product_type("KURTA")
        except Exception as exc:
            failures.append(exc)
    assert len(failures) == 3
    text = capture.text()
    lines = [line for line in capture.lines() if line.get("operation") == "ptd_schema"]
    assert lines and all(line["path"] == "<schema link>" for line in lines)
    for failure in failures:
        chain = [failure, failure.__cause__, failure.__context__]
        text += " ".join(repr(e) + str(e) for e in chain if e is not None)
        report = getattr(failure, "report", None)
        text += report.model_dump_json() if report is not None else ""
    for leak in ("ptd-schemas.fake", "X-Amz-", tokens.token, '"$defs"', "__ptd_schema"):
        assert leak not in text, leak


def test_fetched_definition_is_light_in_repr() -> None:
    fetched = FetchedDefinition(
        "A21TJRUUN4KGV", "KURTA", "LISTING_PRODUCT_ONLY", "en_IN", "U1", "ENFORCED", None, {},
        None, {"$defs": {}}, b"\x00" * 32,
    )  # fmt: skip
    assert "$defs" not in repr(fetched)
