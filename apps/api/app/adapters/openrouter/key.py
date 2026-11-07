"""The OpenRouter key check (SI10 D3; https://openrouter.ai/docs/api-reference/limits).

`fetch_key(api_key)` makes one `GET {OPENROUTER_BASE_URL}/key` with the key as a Bearer token: a
5 s timeout, no retry, no redirect. The answer is the key's credit limit, what is left of it and
what it has used, as Decimals; `limit` and `remaining` are None for an unlimited key.

- 401 / 403 → `KeyRefused`: the only proof that the key itself is bad (D5).
- Anything else that is not a clean 200 with finite, non-negative numbers (429, 5xx, a timeout,
  a connect error, a redirect, a non-JSON or malformed body, NaN, Infinity, a negative or a
  non-numeric value) → `KeyUnavailable`, and the caller leaves the stored row as it was.

Nothing here logs, and no exception carries the key, a header or the body: the messages hold
only the status or the class name.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import httpx

from app.core.config import OPENROUTER_BASE_URL

KEY_URL: Final = f"{OPENROUTER_BASE_URL}/key"
TIMEOUT_S: Final = 5.0


@dataclass(frozen=True, slots=True)
class KeyInfo:
    limit: Decimal | None
    remaining: Decimal | None
    usage: Decimal


class KeyRefused(Exception):  # noqa: N818 - the name the plan uses
    """`/key` answered 401 or 403: the key is invalid or revoked."""


class KeyUnavailable(Exception):  # noqa: N818 - the name the plan uses
    """`/key` gave no usable answer; the stored check stays as it was."""


def _number(data: dict[str, object], name: str, *, nullable: bool) -> Decimal | None:
    if name not in data:
        raise KeyUnavailable(f"missing {name}")
    value = data[name]
    if value is None and nullable:
        return None
    # bool is an int subclass; a string (even "NaN" or "1.5") is not a JSON number
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise KeyUnavailable(f"{name} is not a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise KeyUnavailable(f"{name} is not finite")
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise KeyUnavailable(f"{name} is out of range")
    return number


def _parse(body: object) -> KeyInfo:
    if not isinstance(body, dict):
        raise KeyUnavailable("body is not an object")
    data = body.get("data")
    if not isinstance(data, dict):
        raise KeyUnavailable("data is not an object")
    usage = _number(data, "usage", nullable=False)
    if usage is None:  # unreachable: nullable=False never returns None
        raise KeyUnavailable("usage is null")
    return KeyInfo(
        limit=_number(data, "limit", nullable=True),
        remaining=_number(data, "limit_remaining", nullable=True),
        usage=usage,
    )


async def fetch_key(api_key: str, *, transport: httpx.AsyncBaseTransport | None = None) -> KeyInfo:
    """One `/key` call; see the module docstring for the outcomes."""
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=TIMEOUT_S, follow_redirects=False
        ) as client:
            response = await client.get(KEY_URL, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        raise KeyUnavailable(type(exc).__name__) from None
    if response.status_code in (401, 403):
        raise KeyRefused(f"HTTP {response.status_code}")
    if response.status_code != 200:
        raise KeyUnavailable(f"HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        raise KeyUnavailable("body is not JSON") from None
    return _parse(body)
