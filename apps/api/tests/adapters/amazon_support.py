"""Amazon adapter test helpers (S20): listings pinned to the seeded Amazon mappings."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

from app.adapters.base import AdapterItem, AdapterListing, AdapterTemplate
from mkos_schemas.listing import CanonicalListing
from tests.adapters.contract import family, item, sample_listing


def template(category: str = "ethnic_wear_kurta_sets") -> AdapterTemplate:
    from app.db.seed_templates import seed_templates

    (seed,) = [s for s in seed_templates() if s.template.category == category]
    return AdapterTemplate(seed.product_type, None, seed.template)


def listing(**attributes: Any) -> CanonicalListing:
    base = sample_listing()
    values = {**base.attributes, "fabric_type": "cotton", **attributes}
    return base.model_copy(
        update={"attributes": {k: v for k, v in values.items() if v is not None}}
    )


def image_refs(body: CanonicalListing) -> dict[uuid.UUID, str]:
    return {i.image_id: f"https://images.example.test/{i.image_id}.jpg" for i in body.images}


def amazon_item(
    body: CanonicalListing | None = None,
    *,
    sku: str | None = None,
    key: str | None = None,
    **over: Any,
) -> AdapterListing:
    body = body or listing()
    sent = item(body, sku=sku, key=key)
    fields: dict[str, Any] = {"template": template(), "image_refs": image_refs(body)}
    fields.update(over)
    return dataclasses.replace(sent, **fields)


def amazon_family(
    body: CanonicalListing | None = None,
    *,
    theme: str = "size_color",
    children: list[dict[str, str]] | None = None,
    **over: Any,
) -> AdapterListing:
    body = body or listing()
    sent = family(body)
    parent = sent.units()[0]
    rows = (
        children
        if children is not None
        else [
            {"size": "S", "color": "navy blue"},
            {"size": "S", "color": "maroon"},
            {"size": "M", "color": "navy blue"},
            {"size": "M", "color": "maroon"},
        ]
    )
    kids = tuple(
        AdapterItem(uuid.uuid4(), f"{parent.seller_sku}-{n}", "child", row)
        for n, row in enumerate(rows, start=1)
    )
    fields: dict[str, Any] = {
        "items": (parent, *kids),
        "variation_theme": theme,
        "template": template(),
        "image_refs": image_refs(body),
    }
    fields.update(over)
    return dataclasses.replace(sent, **fields)


def prepare(sent: AdapterListing) -> AdapterListing:
    """The contract suite's `prepare`: what S21's input builder adds for Amazon (the pinned
    kurta mapping and image locators), plus the `fabric_type` the mapping requires, which the
    shared fixture listing does not carry."""
    body = sent.listing
    if "fabric_type" not in body.attributes:
        body = body.model_copy(update={"attributes": {**body.attributes, "fabric_type": "cotton"}})
    return dataclasses.replace(sent, listing=body, template=template(), image_refs=image_refs(body))


# ---- the client's ports over the in-process fake (T10-T12) ----


class FakeTokens:
    """A `TokenSource` over the fake marketplace: tokens the fake accepts, a counted refresh."""

    def __init__(self, app: Any, *, refuse_refresh: bool = False) -> None:
        from tests.support.fake_marketplace import issue_token

        self._app = app
        self._issue = issue_token
        self.token = issue_token(app)
        self.refreshes = 0
        self._issued = {self.token}
        self.refuse_refresh = refuse_refresh

    async def get(self) -> Any:
        from pydantic import SecretStr

        return SecretStr(self.token)

    async def refresh(self) -> Any:
        from app.adapters.amazon.client import failure
        from app.adapters.base import AuthExpired
        from mkos_schemas.codes import ErrorCode
        from pydantic import SecretStr

        self.refreshes += 1
        if self.refuse_refresh:
            raise AuthExpired(failure(ErrorCode.AUTH_EXPIRED, "The refresh was refused."))
        self.token = self._issue(self._app)
        self._issued.add(self.token)
        return SecretStr(self.token)

    def app_tokens(self) -> set[str]:
        """Every token this source handed out (S22 no-leak test)."""
        return set(self._issued)


class CountingLimiter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def acquire(self, operation: str) -> float:
        self.calls.append(operation)
        return float(len(self.calls))


class FixedRng:
    def __init__(self, jitter: float = 0.25) -> None:
        self.jitter = jitter

    def uniform(self, a: float, b: float) -> float:
        return a + self.jitter


SELLER = "A1FAKESELLER"


def spapi_client(app: Any, *, transport: Any = None, tokens: Any = None, **over: Any) -> Any:
    import httpx
    from app.adapters.amazon.client import SpApiClient

    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    client = SpApiClient(
        endpoint="http://fake.test",
        marketplace_id="A21TJRUUN4KGV",
        seller_id=over.pop("seller_id", SELLER),
        tokens=tokens or FakeTokens(app),
        limiter=over.pop("limiter", CountingLimiter()),
        transport=transport or httpx.ASGITransport(app),
        sleep=over.pop("sleep", sleep),
        rng=over.pop("rng", FixedRng()),
        **over,
    )
    client.sleeps = sleeps  # type: ignore[attr-defined]
    return client


def amazon_adapter(app: Any, *, rows: Any = (), **client_over: Any) -> Any:
    from app.adapters.amazon.adapter import AmazonAdapter
    from app.adapters.error_map import ErrorMapper

    return AmazonAdapter(spapi_client(app, **client_over), ErrorMapper(rows))


def attribute_names(path: str) -> list[str]:
    """The Amazon attributes a canonical path feeds, under the kurta mapping."""
    from app.adapters.amazon.mapping import reverse_index

    return [name for name, feeds in reverse_index(template().template).items() if path in feeds]


def reject(app: Any, fail: Any) -> None:
    """Make the fake refuse every attribute the named paths feed, on every SKU."""
    for path, (code, text) in fail.items():
        for name in attribute_names(path):
            app.state.fake.spapi_reject[name] = (code, text)
