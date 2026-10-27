"""S3 T14 (AC2, AC3, AC5), moved to the integration suite by S10 C6: a property test over the whole
`/api/v1` surface (Schemathesis), now against PostgreSQL with real bearers.

Every generated request must get a declared status, content type, headers and a body matching the
declared schema, and never a 5xx: `not_a_server_error` is on since S26 (no stub is left). One run per
role (an owner, operator, reviewer and admin of the seeded `acme`), each with a real access token,
so the role checks, the probe and the limiters run for real; the counters are emptied before
every example. Requests go through one httpx client on one event loop held for the whole test,
so the app's async engine never crosses loops.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
import schemathesis
from app.api.v1.surface import OPS
from hypothesis import HealthCheck, settings
from schemathesis.checks import not_a_server_error
from schemathesis.specs.openapi.checks import (
    content_type_conformance,
    response_headers_conformance,
    response_schema_conformance,
    status_code_conformance,
)
from tests.support.dsn import as_app_rw
from tests.support.rate_limits import reset_rate_limits

from .auth_support import ROLES, Conn, World, build_world, tokens_for
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

BASE_URL: Final = "http://testserver"
# Ops left out of the run, each with its reason; the list shrinks as later steps land.
EXCLUDED_OPS: Final[dict[str, str]] = {}
CHECKS: Final = [
    not_a_server_error,
    status_code_conformance,
    content_type_conformance,
    response_schema_conformance,
    response_headers_conformance,
]


def _text(value: Any) -> bytes | str:
    if isinstance(value, bytes | str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def _file_part(name: str, value: Any) -> tuple[str, Any]:
    """A requests-style multipart entry as httpx takes it: Schemathesis generates booleans,
    numbers and None for form fields, which httpx only takes as text or bytes."""
    if isinstance(value, tuple):
        filename, content, *rest = value
        return name, (filename, _text(content), *rest)
    return name, (None, _text(value))


@dataclass
class Surface:
    runner: asyncio.Runner
    client: httpx.AsyncClient
    world: World
    admin: Conn
    spec: dict[str, Any]

    def send(self, kwargs: dict[str, Any]) -> httpx.Response:
        if kwargs.get("files"):
            kwargs["files"] = [_file_part(name, value) for name, value in kwargs["files"]]
        request = self.client.build_request(**kwargs)
        request.read()  # a multipart body is a stream; the checks read the request back
        return self.runner.run(self.client.send(request))


@pytest.fixture
def surface(
    superuser_conn: Conn,
    clean_tenant_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Surface]:
    from app.core.config import get_settings
    from app.db.session import dispose_engine, get_engine, get_sessionmaker
    from app.main import create_app
    from tests.support.fake_marketplace import build_fake

    from .marketplace_support import CFG, MKT_ENV, install_fake

    admin = os.environ["DATABASE_URL"]
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", admin)
    monkeypatch.setenv("DATABASE_URL", as_app_rw(admin))
    # S26: the marketplaces are configured (on the in-process fake), so connect and callback
    # answer for real instead of 501 NOT_IMPLEMENTED, which `not_a_server_error` would flag
    for key, value in MKT_ENV.items():
        monkeypatch.setenv(key, value)
    install_fake(monkeypatch, build_fake(CFG))
    # DPP-1 (ADR-0021): MFA is on, so the MFA ops answer for real instead of the dormant 501
    # NOT_IMPLEMENTED of `MFA_ENFORCEMENT=off`, which `not_a_server_error` would flag
    monkeypatch.setenv("MFA_ENFORCEMENT", "all")
    monkeypatch.delenv("MFA_GRACE_UNTIL", raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    get_sessionmaker.cache_clear()
    get_engine.cache_clear()
    world = build_world(superuser_conn)
    app = create_app()
    runner = asyncio.Runner()
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 4000))
    client = httpx.AsyncClient(transport=transport, base_url=BASE_URL)
    try:
        yield Surface(runner, client, world, superuser_conn, app.openapi())
    finally:
        runner.run(client.aclose())
        runner.run(dispose_engine())
        runner.close()
        get_settings.cache_clear()


@pytest.fixture
def surface_schema(surface: Surface) -> Any:
    # The registry's operations only: the unversioned probes are not part of the surface.
    loaded = schemathesis.openapi.from_dict(surface.spec)
    return loaded.include(path_regex=r"^/api/v1/")


schema = schemathesis.pytest.from_fixture("surface_schema").include(path_regex=r"^/api/v1/")


def _property_for(role: str) -> Any:
    """The property test as one role (Schemathesis tests take no pytest parametrization)."""

    @schema.parametrize()
    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[
            HealthCheck.too_slow,
            HealthCheck.filter_too_much,
            HealthCheck.function_scoped_fixture,
        ],
    )
    def test(case: Any, surface: Surface) -> None:
        op_id = case.operation.definition.raw.get("operationId")
        if op_id in EXCLUDED_OPS:
            pytest.skip(EXCLUDED_OPS[op_id])
        reset_rate_limits(surface.admin)
        # a fresh TOTP step-up (ADR-0021), so the step-up ops run their own logic, not only 403
        token = tokens_for(
            surface.world, f"{role}@acme.test", "acme", amr=["pwd", "otp"], mfa_at=int(time.time())
        )
        kwargs = case.as_transport_kwargs(
            base_url=BASE_URL, headers={"Authorization": f"Bearer {token}"}
        )
        kwargs["headers"] = dict(kwargs["headers"])
        response = surface.send(kwargs)
        case.validate_response(response, checks=CHECKS)
        assert response.status_code < 500, response.text

    test.__name__ = f"test_surface_property_{role}"
    return test


test_surface_property_owner = _property_for("owner")
test_surface_property_operator = _property_for("operator")
test_surface_property_reviewer = _property_for("reviewer")
test_surface_property_admin = _property_for("admin")


def test_property_runs_every_role() -> None:
    assert {f"test_surface_property_{r}" for r in ROLES} <= set(globals())


def test_excluded_ops_are_known() -> None:
    assert set(OPS) >= set(EXCLUDED_OPS)
    assert all(reason.strip() for reason in EXCLUDED_OPS.values())


def test_property_excludes_no_op_and_checks_server_errors() -> None:
    """S26 T15 (AC 4): every op runs, and a 5xx fails the property."""
    assert EXCLUDED_OPS == {}
    assert not_a_server_error in CHECKS


def test_property_covers_every_op(surface_schema: Any) -> None:
    covered = {op.ok().definition.raw["operationId"] for op in surface_schema.get_all_operations()}
    assert covered == set(OPS)
