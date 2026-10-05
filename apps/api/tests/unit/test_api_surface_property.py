"""S3 T14 (AC2, AC3, AC5): property test over the whole frozen surface (Schemathesis `from_asgi`).

Every generated request must get a declared status, content type, headers and a body matching the
declared schema, and never a 500. `not_a_server_error` is off because 501 is the stub's contract.
Valid only while no route touches a database: S10 C6 moves this file to the integration suite
with real bearers (and `EXCLUDED_OPS` becomes the shrinking per-op list).
"""

from __future__ import annotations

from typing import Any, Final

import pytest
import schemathesis
from app.api.v1.surface import OPS
from fastapi import FastAPI
from hypothesis import HealthCheck, settings
from schemathesis.checks import not_a_server_error
from schemathesis.specs.openapi.checks import (
    content_type_conformance,
    response_headers_conformance,
    response_schema_conformance,
    status_code_conformance,
)
from tests.api_requests import BEARER

EXCLUDED_OPS: Final[frozenset[str]] = frozenset()
CHECKS: Final = [
    status_code_conformance,
    content_type_conformance,
    response_schema_conformance,
    response_headers_conformance,
]


@pytest.fixture(scope="module")
def surface_schema(v1_app: FastAPI) -> Any:
    # The registry's operations only: the unversioned probes are not part of the surface.
    loaded = schemathesis.openapi.from_asgi("/openapi.json", v1_app)
    return loaded.include(path_regex=r"^/api/v1/")


schema = schemathesis.pytest.from_fixture("surface_schema").include(path_regex=r"^/api/v1/")


@schema.parametrize()
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
def test_surface_property(case: Any) -> None:
    if case.operation.definition.raw.get("operationId") in EXCLUDED_OPS:
        pytest.skip("excluded until its owner step's integration run")
    response = case.call_and_validate(
        headers=dict(BEARER), checks=CHECKS, excluded_checks=[not_a_server_error]
    )
    assert response.status_code != 500


def test_excluded_ops_are_known() -> None:
    assert set(OPS) >= EXCLUDED_OPS


def test_property_covers_every_op(surface_schema: Any) -> None:
    covered = {op.ok().definition.raw["operationId"] for op in surface_schema.get_all_operations()}
    assert covered == set(OPS)
