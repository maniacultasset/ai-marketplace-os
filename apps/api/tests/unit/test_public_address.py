"""G2: the one public-address check (`app.core.public_address`), shared by the PTD schema link and
the sheet photo fetcher. The full blocked list is in `test_photo_fetch.py`."""

from __future__ import annotations

import pytest
from app.core.public_address import is_public_address


@pytest.mark.parametrize(
    "address",
    [
        "64:ff9b::808:808",  # NAT64 of a public address: still refused (the prefix tunnels)
        "2002:808:808::1",  # 6to4 of a public address
        "::808:808",  # IPv4-compatible
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
    ],
)
def test_tunnel_prefixes_are_refused_whatever_they_embed(address: str) -> None:
    assert not is_public_address(address)


def test_the_amazon_definitions_client_uses_the_shared_check() -> None:
    from app.adapters.amazon import definitions

    assert vars(definitions)["is_public_address"] is is_public_address
