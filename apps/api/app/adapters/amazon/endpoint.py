"""The SP-API endpoint, pinned to `AMAZON_USE_SANDBOX` (ENV.md:101-104; S20 T16; PLAN D10; C17).

Stdlib only, so `app.core.config` can run it at boot. The endpoint's scheme and host must be
`SANDBOX_HOST` while the sandbox flag is true and `PROD_HOST` once it is false, with no port,
path, userinfo, query or fragment: a staging deploy can never write to the production
marketplace by a mistyped pair. Only `APP_ENV=local` may point at a loopback host instead (the
fake marketplace). Both hosts are believed from the SP-API docs (EU region group, India
included); DW1 confirms them. Messages name the two variables, never a value.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

SANDBOX_HOST: Final = "https://sandbox.sellingpartnerapi-eu.amazon.com"
PROD_HOST: Final = "https://sellingpartnerapi-eu.amazon.com"
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})
_MISMATCH: Final = (
    "AMAZON_SPAPI_ENDPOINT must be the SP-API {which} host while AMAZON_USE_SANDBOX={flag}"
)
_SHAPE: Final = (
    "AMAZON_SPAPI_ENDPOINT must be a bare https origin (no port, path, userinfo, query or "
    "fragment)"
)


def spapi_endpoint_errors(endpoint: str, *, use_sandbox: bool, app_env: str) -> list[str]:
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return [_SHAPE]
    bare = (
        parts.path in ("", "/")
        and not parts.query
        and not parts.fragment
        and parts.username is None
        and parts.password is None
        and "@" not in parts.netloc
    )
    if app_env == "local" and host in LOOPBACK_HOSTS and parts.scheme in ("http", "https"):
        return [] if bare else [_SHAPE]
    if not bare or port is not None or parts.scheme != "https" or host is None:
        return [_SHAPE]
    origin = f"{parts.scheme}://{host}"
    expected = SANDBOX_HOST if use_sandbox else PROD_HOST
    if origin != expected:
        which = "sandbox" if use_sandbox else "production"
        return [_MISMATCH.format(which=which, flag=str(use_sandbox).lower())]
    return []
