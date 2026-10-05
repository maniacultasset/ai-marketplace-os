"""S3 T15 (AC9): an implemented route ships its whole case matrix and drops presence-only auth."""

from __future__ import annotations

from collections import defaultdict
from typing import NoReturn

import pytest
from app.api.errors import NotImplementedYet
from app.api.v1.surface import OPS, cases, is_stub, route, stub
from fastapi import APIRouter, FastAPI
from tests.endpoint_cases import (
    REQUIRED_VARIANTS,
    Found,
    MarkerError,
    guard_failures,
    missing_cases,
    scan,
    scan_source,
)


def test_implemented_routes_have_case_matrix(v1_app: FastAPI) -> None:
    failures = [f for f in guard_failures(v1_app, scan()) if "presence-only" not in f]
    assert failures == []


def test_implemented_routes_not_presence_only(v1_app: FastAPI) -> None:
    failures = [f for f in guard_failures(v1_app, scan()) if "presence-only" in f]
    assert failures == []


def test_every_route_is_a_stub_in_s3(v1_app: FastAPI) -> None:
    from fastapi.routing import iter_route_contexts

    endpoints = {
        c.operation_id: c.endpoint
        for c in iter_route_contexts(v1_app.routes)
        if getattr(c, "operation_id", None)
    }
    assert set(endpoints) == set(OPS)
    assert all(is_stub(fn) for fn in endpoints.values())


def _app_with(fn_is_stub: bool) -> FastAPI:
    router = APIRouter()

    async def auth_me() -> NoReturn:
        raise NotImplementedYet("auth_me")

    handler = stub(auth_me) if fn_is_stub else auth_me
    route(router, OPS["auth_me"])(handler)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


def _all_cases(op_id: str) -> Found:
    found: Found = defaultdict(lambda: defaultdict(set))
    for case in cases(OPS[op_id]):
        variants: set[str | None] = set(REQUIRED_VARIANTS.get(case, {None}))
        found[op_id][case] |= variants
    return found


def test_guard_flags_a_route_without_its_cases() -> None:
    failures = guard_failures(_app_with(fn_is_stub=False), scan_dir_empty())
    assert any(f.startswith("auth_me: missing endpoint cases: ") for f in failures)
    message = next(f for f in failures if "missing endpoint cases" in f)
    for needed in ("success", "missing_auth", "wrong_tenant", "invalid_token[expired]"):
        assert needed in message


def test_guard_flags_presence_only_bearer() -> None:
    failures = guard_failures(_app_with(fn_is_stub=False), _all_cases("auth_me"))
    assert failures == ["auth_me: presence-only bearer_required"]


def test_guard_skips_stubs() -> None:
    assert guard_failures(_app_with(fn_is_stub=True), scan_dir_empty()) == []


def scan_dir_empty() -> Found:
    return defaultdict(lambda: defaultdict(set))


def test_missing_cases_needs_each_variant() -> None:
    found = _all_cases("listings_approve")
    assert missing_cases(OPS["listings_approve"], found) == []
    found["listings_approve"]["maker_checker"].discard("editor_approves")
    assert missing_cases(OPS["listings_approve"], found) == ["maker_checker[editor_approves]"]
    callback = _all_cases("marketplaces_callback")
    callback["marketplaces_callback"]["bad_state"].discard("replayed")
    assert missing_cases(OPS["marketplaces_callback"], callback) == ["bad_state[replayed]"]


def test_scanner_reads_literal_markers() -> None:
    source = (
        "import pytest\n"
        '@pytest.mark.endpoint("auth_me", case="success")\n'
        "def test_a(): ...\n"
        '@pytest.mark.endpoint("auth_me", case="invalid_token", variant="expired")\n'
        "def test_b(): ...\n"
    )
    assert scan_source(source, "x.py") == [
        ("auth_me", "success", None),
        ("auth_me", "invalid_token", "expired"),
    ]


@pytest.mark.parametrize(
    "marker",
    [
        "pytest.mark.endpoint(OP, case='success')",
        "pytest.mark.endpoint('auth_me', case=CASE)",
        "pytest.mark.endpoint('auth_me', case='invalid_token', variant=V)",
        "pytest.mark.endpoint('auth_me', case=f'succ{1}')",
        "pytest.mark.endpoint('auth_me')",
        "pytest.mark.endpoint('auth_me', 'success')",
        "pytest.mark.endpoint('auth_me', case='success', **extra)",
        "pytest.mark.endpoint('auth_me', case='success', other='x')",
    ],
)
def test_scanner_rejects_non_literal_marker(marker: str) -> None:
    with pytest.raises(MarkerError):
        scan_source(f"import pytest\n@{marker}\ndef test_x(): ...\n", "x.py")


@pytest.mark.parametrize(
    "marker",
    [
        "pytest.mark.endpoint('no_such_op', case='success')",
        "pytest.mark.endpoint('auth_me', case='no_such_case')",
        "pytest.mark.endpoint('jobs_dead_letter', case='success')",
    ],
)
def test_scanner_unknown_op_or_case_fails(marker: str) -> None:
    with pytest.raises(MarkerError):
        scan_source(f"import pytest\n@{marker}\ndef test_x(): ...\n", "x.py")


def test_scan_of_the_repo_parses() -> None:
    scan()
