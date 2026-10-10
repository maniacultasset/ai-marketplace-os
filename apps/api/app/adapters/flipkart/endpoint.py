"""The Flipkart API base and OAuth URLs, pinned (S22 T2; PLAN D8; C10). Believed, DW1.

Stdlib only, so `app.core.config` can run it at boot (flag on, off local), and the token refresh
and `open_adapter` repeat it before any unseal or call in every environment.

- `flipkart_endpoint_errors(api_base, *, app_env)`: off local `FLIPKART_API_BASE` must be exactly
  `API_HOST` (an optional trailing `/` aside): https, no port, userinfo, query or fragment.
- `flipkart_oauth_endpoint_errors(token_url, authorize_url, *, app_env)`: off local
  `FLIPKART_TOKEN_URL` must equal `TOKEN_URL` (ENV.md), and `FLIPKART_AUTHORIZE_URL` (when set)
  must be https on a host in `OAUTH_HOSTS`, with no port, userinfo or fragment.
- `APP_ENV=local` may also use a loopback http(s) URL (the fake marketplace), never with userinfo,
  a query or a fragment. A non-loopback host on local is held to the pin like any other env.

Messages name the variables only, never a value.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import SplitResult, urlsplit

API_HOST: Final = "https://api.flipkart.net/sellers"
TOKEN_URL: Final = "https://api.flipkart.net/oauth-service/oauth/token"  # noqa: S105 - a URL
OAUTH_HOSTS: Final = frozenset({"api.flipkart.net"})
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})
_API_MSG: Final = (
    f"FLIPKART_API_BASE must be {API_HOST} (no port, userinfo, query or fragment) while "
    "FLIPKART_ENABLED=true"
)
_TOKEN_MSG: Final = f"FLIPKART_TOKEN_URL must be {TOKEN_URL} while FLIPKART_ENABLED=true"
_AUTHORIZE_MSG: Final = (
    "FLIPKART_AUTHORIZE_URL must be https on a pinned Flipkart host (no port, userinfo or "
    "fragment) while FLIPKART_ENABLED=true"
)


def _split(value: str) -> tuple[SplitResult, str | None, int | None] | None:
    try:
        parts = urlsplit(value.strip())
        return parts, parts.hostname, parts.port
    except ValueError:
        return None


def _no_userinfo(parts: SplitResult) -> bool:
    return parts.username is None and parts.password is None and "@" not in parts.netloc


def _local_loopback(value: str, app_env: str) -> bool | None:
    """True: a usable local loopback URL; False: a loopback URL with a bad shape; None: not a
    local loopback URL (the pin applies)."""
    split = _split(value)
    if app_env != "local" or split is None:
        return None
    parts, host, _ = split
    if host not in LOOPBACK_HOSTS or parts.scheme not in ("http", "https"):
        return None
    return _no_userinfo(parts) and not parts.query and not parts.fragment and "#" not in value


def flipkart_endpoint_errors(api_base: str, *, app_env: str) -> list[str]:
    local = _local_loopback(api_base, app_env)
    if local is not None:
        return [] if local else [_API_MSG]
    split = _split(api_base)
    if split is None:
        return [_API_MSG]
    parts, host, port = split
    exact = (
        parts.scheme == "https"
        and host == "api.flipkart.net"
        and port is None
        and _no_userinfo(parts)
        and parts.netloc == "api.flipkart.net"
        and parts.path.rstrip("/") == "/sellers"
        and not parts.query
        and not parts.fragment
        and "#" not in api_base
        and "?" not in api_base
    )
    return [] if exact else [_API_MSG]


def _token_errors(token_url: str, app_env: str) -> list[str]:
    local = _local_loopback(token_url, app_env)
    if local is not None:
        return [] if local else [_TOKEN_MSG]
    return [] if token_url.strip() == TOKEN_URL else [_TOKEN_MSG]


def _authorize_errors(authorize_url: str | None, app_env: str) -> list[str]:
    if authorize_url is None:
        return []
    local = _local_loopback(authorize_url, app_env)
    if local is not None:
        return [] if local else [_AUTHORIZE_MSG]
    split = _split(authorize_url)
    if split is None:
        return [_AUTHORIZE_MSG]
    parts, host, port = split
    ok = (
        parts.scheme == "https"
        and host in OAUTH_HOSTS
        and parts.netloc == host
        and port is None
        and _no_userinfo(parts)
        and not parts.fragment
        and "#" not in authorize_url
    )
    return [] if ok else [_AUTHORIZE_MSG]


def flipkart_oauth_endpoint_errors(
    token_url: str, authorize_url: str | None, *, app_env: str
) -> list[str]:
    return _token_errors(token_url, app_env) + _authorize_errors(authorize_url, app_env)
