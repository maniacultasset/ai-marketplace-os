"""Exit 0 when argv[1] is a local plain-HTTP base URL, else 2 (CODE-PLAN S12 T14, L9).

The e2e scripts and the limiter reset use the public local seed password and the local compose
database, so they refuse anything but `http://127.0.0.1[:port]` or `http://localhost[:port]`
with no userinfo, path tricks or query. Stdlib only; prints nothing but a fixed reason."""

from __future__ import annotations

import sys
from urllib.parse import urlsplit

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})


def is_local(url: str) -> bool:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "http" or parts.username is not None or parts.password is not None:
        return False
    if "@" in parts.netloc or parts.query or parts.fragment or parts.path not in ("", "/"):
        return False
    if port is not None and not 1 <= port <= 65535:
        return False
    return parts.hostname in LOCAL_HOSTS


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not is_local(argv[1]):
        sys.stderr.write("refused: the base URL must be http://127.0.0.1 or http://localhost\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
