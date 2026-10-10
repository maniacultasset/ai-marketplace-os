"""S26 T7 (AC 2, OWASP API7): server-side request forgery.

A URL in a CSV cell, a product field or listing content is stored as text and never opened: the
`socket_spy` sees no connection to it while the import job runs. A storage key that is a URL is
refused. Statically, over all of `app/**`: only the reviewed outbound-client modules build an
HTTP client, each takes its host from `Settings` (or a pinned constant), none follows redirects,
and nothing calls the module-level `httpx.get/post/request/stream`.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import httpx
import pytest
from tests.integration.imports_support import MAPPED, csv_bytes, row, run_job, seed_templates

from .conftest import World2

APP = Path(__file__).resolve().parents[2] / "app"
HOSTILE = ("http://127.0.0.1:1/x.jpg", "http://169.254.169.254/latest/meta-data/",
           "file:///etc/passwd", "http://[::1]:1/x.jpg")  # fmt: skip
# The modules that build an outbound HTTP client, and where each takes its host from: the file
# that supplies the URL and the Settings attribute (or pinned constant) it reads.
OUTBOUND_CLIENTS = {
    "core/slack.py": ("workers/alerting.py", "alert_slack_webhook"),
    "adapters/oauth/http.py": ("adapters/oauth/__init__.py", "s.amazon_lwa_token_url"),
    "adapters/amazon/client.py": ("workers/adapters.py", "amazon_spapi_endpoint"),
    "services/embeddings.py": ("services/embeddings.py", "OPENAI_BASE_URL"),
}
CLIENT_CTORS = {"Client", "AsyncClient", "AsyncOpenAI"}
MODULE_CALLS = {"get", "post", "put", "patch", "delete", "request", "stream", "head", "options"}


def _hostile_targets(seen: list[tuple[str, int]]) -> list[tuple[str, int]]:
    bad = {"169.254.169.254", "::1", "[::1]"}
    return [t for t in seen if t[0] in bad or t[1] == 1]


@pytest.mark.parametrize("url", HOSTILE, ids=["loopback", "metadata", "file", "ipv6"])
async def test_api7_urls_in_csv_open_no_socket(
    api: httpx.AsyncClient, world2: World2, socket_spy: list[tuple[str, int]], url: str
) -> None:
    seed_templates(world2.base.conn)
    data = csv_bytes(
        [row(1, main_image=url, description=f"see {url}", category=MAPPED[0])],
    )
    response = await api.post(
        "/api/v1/products/bulk",
        headers={**world2.headers("operator@acme.test"), "Idempotency-Key": str(uuid.uuid4())},
        files={"file": ("ssrf.csv", data, "text/csv")},
    )
    assert response.status_code in (202, 422), response.text
    if response.status_code == 202:
        await run_job(uuid.UUID(response.json()["job_id"]))
    assert _hostile_targets(socket_spy) == [], socket_spy


async def test_api7_urls_in_product_and_content_fields_open_no_socket(
    api: httpx.AsyncClient, world2: World2, socket_spy: list[tuple[str, int]]
) -> None:
    headers = world2.headers("operator@acme.test")
    for url in HOSTILE:
        created = await api.post(
            "/api/v1/products",
            headers=headers,
            json={"sku": f"SSRF-{uuid.uuid4().hex[:8]}", "title": url, "category": "kurtas"},
        )
        assert created.status_code in (201, 422), created.text
    dry = await api.post(
        "/api/v1/rules/dry-run",
        headers=headers,
        json={
            "listing": {
                "title": HOSTILE[1],
                "bullets": [HOSTILE[0]],
                "description": HOSTILE[2],
                "category": "kurtas",
                "marketplace": "amazon",
            }
        },
    )
    assert dry.status_code < 500, dry.text
    assert _hostile_targets(socket_spy) == [], socket_spy


@pytest.mark.parametrize("key", [*HOSTILE, "https://evil.example/x.jpg"])
async def test_api7_storage_key_with_url_refused_422(
    api: httpx.AsyncClient, world2: World2, socket_spy: list[tuple[str, int]], key: str
) -> None:
    response = await api.post(
        "/api/v1/images/upload",
        headers={**world2.headers("operator@acme.test"), "Idempotency-Key": str(uuid.uuid4())},
        data={
            "product_id": str(world2.side("acme").ids["product_id"]),
            "slot": "main",
            "storage_key": key,
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"
    assert _hostile_targets(socket_spy) == []


def _modules() -> list[tuple[str, ast.Module]]:
    out = []
    for path in sorted(APP.rglob("*.py")):
        out.append((str(path.relative_to(APP)), ast.parse(path.read_text(encoding="utf-8"))))
    return out


def _callee(node: ast.Call) -> tuple[str | None, str | None]:
    """(owner name, attribute) of `owner.attr(...)`, or (None, name) of `name(...)`."""
    func = node.func
    if isinstance(func, ast.Attribute):
        owner = func.value.id if isinstance(func.value, ast.Name) else None
        return owner, func.attr
    if isinstance(func, ast.Name):
        return None, func.id
    return None, None


def test_api7_outbound_hosts_only_from_settings() -> None:
    builders: set[str] = set()
    module_calls: list[str] = []
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            owner, attr = _callee(node)
            if attr in CLIENT_CTORS and owner in (None, "httpx", "httpx2", "openai"):
                builders.add(rel)
            if owner in ("httpx", "httpx2", "requests", "urllib") and attr in MODULE_CALLS:
                module_calls.append(f"{rel}:{node.lineno}")
            if owner == "urllib" or (owner == "request" and attr == "urlopen"):
                module_calls.append(f"{rel}:{node.lineno}")
    assert module_calls == [], f"module-level HTTP calls: {module_calls}"
    assert builders == set(OUTBOUND_CLIENTS), (
        "a new outbound HTTP client needs a review row in OUTBOUND_CLIENTS",
        sorted(builders),
    )
    for rel, (source, token) in OUTBOUND_CLIENTS.items():
        assert token in (APP / source).read_text(encoding="utf-8"), (rel, source, token)
    for _, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in ("urllib.request", "requests"):
                raise AssertionError(f"{node.module} imported in app/**")


def test_api7_follow_redirects_never_true() -> None:
    offenders = []
    explicit = 0
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "follow_redirects":
                    explicit += 1
                    if not (
                        isinstance(keyword.value, ast.Constant) and keyword.value.value is False
                    ):
                        offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], offenders
    assert explicit >= 3  # slack, the OAuth client, the SP-API client
