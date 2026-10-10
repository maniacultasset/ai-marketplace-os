"""S26 re-check (tester): every non-served method on every static `/api/v1` path answers 405 with
that path's own `Allow`, never a 422 from a parameter route that shadows it, a 404 or a 2xx."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.api.v1.surface import OPS
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.api_requests import BEARER, PREFIX

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_STATIC: dict[str, set[str]] = {}
for _op in OPS.values():
    if "{" not in _op.path:
        _STATIC.setdefault(PREFIX + _op.path, set()).add(_op.method)
CASES = [
    (m, p, frozenset(s))
    for p, s in sorted(_STATIC.items())
    for m in METHODS
    if m not in s and not (m == "HEAD" and "GET" in s)
]


@pytest.fixture(scope="module")
def client(v1_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(v1_app, raise_server_exceptions=False) as c:
        yield c


@pytest.mark.parametrize(("method", "path", "served"), CASES)
def test_unserved_method_on_static_path_is_405(
    client: TestClient, method: str, path: str, served: frozenset[str]
) -> None:
    response = client.request(method, path, headers=BEARER)
    assert response.status_code == 405, (method, path, response.status_code, response.text[:200])
    allow = set(response.headers["allow"].replace(" ", "").split(","))
    assert served <= allow, (method, path, allow)
    assert method not in allow
