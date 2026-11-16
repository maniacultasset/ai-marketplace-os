"""The sheet photo fetcher (G2): the only code that opens a seller's photo link
(`app.adapters.photo_fetch.fetcher`, whose docstring lists every guard)."""

from app.adapters.photo_fetch.fetcher import (
    HEADERS,
    USER_AGENT,
    FetchFailure,
    FetchOk,
    Limits,
    PhotoFetcher,
    Problem,
    Resolver,
    check_url,
    default_resolver,
)

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
    "default_resolver",
]
