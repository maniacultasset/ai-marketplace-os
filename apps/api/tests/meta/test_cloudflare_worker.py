"""ADR-0025 (EDGE-CF T4): the Cloudflare Worker source. Text checks only (no JS runtime in CI):
the header names match the app's, the secret and the client IP overwrite any client copy, the
target never leaves the fixed origin, and only the edge host is served."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from app.api.origin_auth import CLIENT_IP_HEADER, HEADER

WORKER = "scripts/deploy/cloudflare/api-edge-worker.js"


@pytest.fixture
def source(repo_root: Path) -> str:
    return (repo_root / WORKER).read_text(encoding="utf-8")


def _code(source: str) -> str:
    """The source without `//` line comments (a URL inside a string keeps its `//`)."""
    return "\n".join(re.sub(r"(^|\s)//.*$", "", line) for line in source.splitlines())


def test_worker_sets_the_app_header_names(source: str) -> None:
    code = _code(source)
    assert f'headers.set("{HEADER.decode()}", env.ORIGIN_SECRET)' in code
    assert (
        f'headers.set("{CLIENT_IP_HEADER.decode()}", request.headers.get("cf-connecting-ip")'
        in code
    )


def test_worker_reads_config_from_env(source: str) -> None:
    code = _code(source)
    for name in ("ORIGIN_URL", "EDGE_HOST", "ORIGIN_SECRET"):
        assert f"env.{name}" in code


def test_worker_fails_closed_without_config(source: str) -> None:
    code = _code(source)
    assert re.search(
        r"if \(!env\.ORIGIN_URL \|\| !env\.EDGE_HOST \|\| !env\.ORIGIN_SECRET\) \{\s*"
        r"return new Response\(null, \{ status: 503 \}\);",
        code,
    )


def test_worker_checks_edge_host(source: str) -> None:
    code = _code(source)
    check = code.find("url.hostname !== env.EDGE_HOST")
    assert check != -1
    assert check < code.find("headers.set(")
    assert re.search(
        r"url\.hostname !== env\.EDGE_HOST\) \{\s*return new Response\(null, \{ status: 404 \}\)",
        code,
    )


def test_worker_never_resolves_relative(source: str) -> None:
    assert not re.search(r"new URL\([^)]*,", _code(source))


def test_worker_checks_target_origin(source: str) -> None:
    code = _code(source)
    assert "const base = new URL(env.ORIGIN_URL);" in code
    assert "target.pathname = url.pathname;" in code and "target.search = url.search;" in code
    assert re.search(
        r"if \(target\.origin !== base\.origin\) \{\s*return new Response\(null, \{ status: 502 \}\)",
        code,
    )
    assert code.find("target.origin !== base.origin") < code.find("return fetch(")


def test_worker_keeps_redirects_manual(source: str) -> None:
    assert 'redirect: "manual"' in _code(source)


def test_worker_drops_host_and_writes_no_xff(source: str) -> None:
    code = _code(source).lower()
    assert 'headers.delete("host")' in code
    assert "x-forwarded-for" not in code


def test_worker_logs_nothing(source: str) -> None:
    assert "console." not in _code(source)


def test_worker_holds_no_secret_shaped_literal(source: str) -> None:
    assert not re.search(r"[0-9a-fA-F]{32,}", source)
    assert not re.search(r"[A-Za-z0-9+/=_-]{32,}", source)
