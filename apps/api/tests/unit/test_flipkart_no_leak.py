"""S22 T17 (AC11; invariant 6): the Flipkart token never reaches a log line, a Sentry event, an
exception's text, an ErrorReport, a result or a preview body, on any client or adapter path."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from tests.adapters.flipkart_support import FakeTokens, fk_family, fk_item, flipkart_adapter
from tests.support.fake_marketplace import CANARY, build_fake
from tests.support.fault_transport import FaultTransport


async def _control(app: Any, **body: Any) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fake.test"
    ) as client:
        await client.post("/__control", json=body)


async def test_flipkart_tokens_never_leak(log_lines: Any, sentry_events: Any) -> None:
    import sentry_sdk
    from app.adapters.base import AdapterError
    from app.core.config import Settings
    from app.core.observability import init_sentry

    init_sentry(Settings(_env_file=None, app_env="local", app_role="shell", sentry_dsn="https://k@o0.ingest.sentry.io/1"))  # fmt: skip
    capture = log_lines()
    texts: list[str] = []

    # ok, partial (the error text echoes the token), warning, update, get
    app = build_fake()
    tokens = FakeTokens(app)
    adapter = flipkart_adapter(app, tokens=tokens)
    await _control(app, fk_reject_attribute={"title": ["FK-X", f"bad title {tokens.token}"]})
    sent = fk_item(sku="FK-LEAK")
    texts.append((await adapter.create_listing(sent)).model_dump_json())
    texts.append((await adapter.update_listing(sent, fields=["content.title"])).model_dump_json())
    texts.append((await adapter.get_listing("FK-LEAK")).model_dump_json())
    texts += [repr(adapter), repr(adapter._client)]
    preview = adapter.preview(fk_family())
    texts.append(repr(preview))

    # the raised paths: denied after a refresh, 429 past the budget, ambiguous, unavailable
    raised: list[AdapterError] = []
    for control in ({"fk_auth": {"mode": "denied", "times": 5}}, {"fk_429": 10}):
        app = build_fake()
        tokens = FakeTokens(app)
        await _control(app, **control)
        with pytest.raises(AdapterError) as caught:
            await flipkart_adapter(app, tokens=tokens).create_listing(fk_item())
        raised.append(caught.value)
    app = build_fake()
    for fault in (
        FaultTransport(httpx.ASGITransport(app), after_write=httpx.ReadTimeout("t")),
        FaultTransport(httpx.ASGITransport(app), before_send=httpx.ConnectError("c")),
    ):
        with pytest.raises(AdapterError) as caught:
            await flipkart_adapter(app, transport=fault).create_listing(fk_item())
        raised.append(caught.value)
    for exc in raised:
        sentry_sdk.capture_exception(exc)
        texts += [str(exc), repr(exc), exc.report.model_dump_json()]
    sentry_sdk.get_client().flush(timeout=2)

    issued = app.state.fake.access_tokens | tokens.app_tokens()
    assert issued and all(t.startswith(CANARY) for t in issued)
    for text in (*texts, capture.text(), sentry_events.serialized()):
        assert CANARY not in text
