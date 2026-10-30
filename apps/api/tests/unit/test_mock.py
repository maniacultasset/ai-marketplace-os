"""S3 T20 (AC12): the local mock serves openapi.json examples on loopback only."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app_mock.__main__ import main as mock_main
from app_mock.server import create_mock_app
from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parents[4]
SPEC = REPO / "packages/schemas/jsonschema/openapi.json"
P = "/api/v1"
PID = "0b1c2d3e-0000-4000-8000-000000000101"


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(SPEC.read_text("utf-8"))
    return data


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(create_mock_app(SPEC)) as c:
        yield c


def _ops(spec: dict[str, Any]) -> list[tuple[str, str]]:
    return sorted(
        (method.upper(), path)
        for path, item in spec["paths"].items()
        if path.startswith(P + "/")
        for method in item
    )


def test_mock_products_fixture(client: TestClient) -> None:
    response = client.get(f"{P}/products")
    assert response.status_code == 200
    body = response.json()
    assert len(body["data"]) >= 1
    assert body["data"][0]["sku"]
    # FastAPI drops null-valued keys from examples (exclude_none), so `next_cursor: null` is absent.
    assert {"has_more", "limit"} <= set(body["page"]) <= {"next_cursor", "has_more", "limit"}


def test_mock_prefer_404(client: TestClient) -> None:
    response = client.get(f"{P}/products/{PID}", headers={"Prefer": "code=404"})
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NOT_FOUND"


def test_mock_prefer_undeclared_422(client: TestClient) -> None:
    response = client.get(f"{P}/products", headers={"Prefer": "code=404"})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["type"] == "VALIDATION_FAILED"
    assert "code=404 is not declared" in error["message"]


def test_mock_unknown_path_404(client: TestClient) -> None:
    response = client.get(f"{P}/nope")
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NOT_FOUND"


def test_mock_wrong_method_405(client: TestClient) -> None:
    response = client.delete(f"{P}/products")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET, POST"


def test_mock_literal_beats_template(client: TestClient) -> None:
    validate = client.post(f"{P}/listings/validate")
    assert validate.status_code == 200
    assert validate.json()["passed"] is False


def test_mock_callback_303(client: TestClient) -> None:
    response = client.get(f"{P}/marketplaces/amazon/callback", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/marketplaces/connected?pending=")


def test_mock_every_op_answers(client: TestClient, spec: dict[str, Any]) -> None:
    ops = _ops(spec)
    assert (
        len(ops) == 71
    )  # S3 35, S10 3, S15 2, S16 3, S20 1, S19 4, S21 2, ADR-0019 5, DPP-1 10, ADR-0020 6
    for method, template in ops:
        path = template.replace("{marketplace}", "amazon")
        for name in ("product_id", "import_id", "listing_id", "image_id", "job_id", "user_id"):
            path = path.replace("{" + name + "}", PID)
        response = client.request(method, path, follow_redirects=False)
        declared = spec["paths"][template][method.lower()]["responses"]
        success = [s for s in declared if s.isdigit() and int(s) < 400]
        assert [str(response.status_code)] == success, (method, template)
        for status in (s for s in declared if s.isdigit() and int(s) >= 400):
            preferred = client.request(method, path, headers={"Prefer": f"code={status}"})
            assert preferred.status_code == int(status), (method, template, status)
            assert preferred.json() == declared[status]["content"]["application/json"]["example"]


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "::", "192.168.1.5", "example.com", "localhost"]  # noqa: S104
)
def test_mock_refuses_public_host(host: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert mock_main(["--host", host, "--port", "4010", "--spec", str(SPEC)]) == 2
    assert "loopback only" in capsys.readouterr().err


def test_mock_never_imports_app() -> None:
    import ast

    for path in (REPO / "apps/api/app_mock").glob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not (name == "app" or name.startswith("app.")), (path, name)
