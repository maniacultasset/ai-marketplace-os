"""ADR-0019: self-serve sign-up with platform-admin approval, without a database.

The request models (mass assignment, validators), the slug derivation, the decision table, the
registry rows and their policy, the platform-admin dependency, the service flows over fake
repositories, the `user make-admin` arguments and the 0010 revision's shape. The database half
(RLS, the trigger, the HTTP flows) is in tests/integration/test_signup_api.py and
tests/tenant_isolation/test_signup_isolation.py.
"""

from __future__ import annotations

import importlib.util
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from app.api.errors import install_error_handlers
from app.api.v1.schemas.auth import MeOut
from app.api.v1.schemas.signup import (
    SignupApprove,
    SignupCreate,
    SignupReject,
    SignupStatusRequest,
)
from app.api.v1.surface import NO_ROLES, OPS, cases, declared_responses
from app.core.errors import ConflictError, NotFoundError
from app.core.rbac import PLATFORM_ADMIN, Action, Principal, route_policy
from app.repositories.identity import SignupRow, SlugTakenError, UserRow
from app.services import signup as svc
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from mkos_schemas.codes import ErrorCode
from pydantic import ValidationError

TENANT_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
GOOD: dict[str, Any] = {
    "brand_name": "Kalakar Weaves",
    "owner_name": "Meera Iyer",
    "email": "Founder@Kalakar.Example",
    "password": "a-long-enough-pass",
}


# ---- request models ----


def test_signup_minimal_body_valid_and_email_lowered() -> None:
    body = SignupCreate.model_validate(GOOD)
    assert body.email == "founder@kalakar.example"
    assert body.phone is None and body.website is None
    assert body.password.get_secret_value() == "a-long-enough-pass"


def test_signup_optional_fields_valid() -> None:
    body = SignupCreate.model_validate(
        {**GOOD, "phone": "+91 98765 43210", "website": "https://kalakar.example/shop"}
    )
    assert body.phone == "+91 98765 43210"
    assert body.website == "https://kalakar.example/shop"


@pytest.mark.parametrize(
    "field_name", ["is_platform_admin", "status", "tenant_id", "role", "decided_by_user_id"]
)
def test_signup_mass_assignment_refused(field_name: str) -> None:
    with pytest.raises(ValidationError):
        SignupCreate.model_validate({**GOOD, field_name: True})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("brand_name", ""),
        ("brand_name", "   "),
        ("brand_name", "x" * 201),
        ("brand_name", "bad\x00name"),
        ("owner_name", ""),
        ("email", "no-at-sign"),
        ("email", "a@b@c.test"),
        ("email", "o" + chr(0xFEFF) + "@kalakar.example"),
        ("email", "x" * 250 + "@k.example"),
        ("password", "short-pass"),
        ("password", "x" * 1025),
        ("phone", "12"),
        ("phone", "call me maybe"),
        ("website", "ftp://kalakar.example"),
        ("website", "https://localhost"),
        ("website", "javascript:alert(1)"),
    ],
)
def test_signup_invalid_field_422(key: str, value: str) -> None:
    with pytest.raises(ValidationError):
        SignupCreate.model_validate({**GOOD, key: value})


@pytest.mark.parametrize("missing", ["brand_name", "owner_name", "email", "password"])
def test_signup_required_fields(missing: str) -> None:
    with pytest.raises(ValidationError):
        SignupCreate.model_validate({k: v for k, v in GOOD.items() if k != missing})


def test_status_request_shape() -> None:
    SignupStatusRequest.model_validate({"email": "a@b.example", "password": "x"})
    with pytest.raises(ValidationError):
        SignupStatusRequest.model_validate({"email": "a@b.example", "password": ""})


@pytest.mark.parametrize("days", [0, 366, 1.5, "x"])
def test_approve_trial_days_bounds(days: Any) -> None:
    with pytest.raises(ValidationError):
        SignupApprove.model_validate({"trial_days": days})


def test_approve_body_optional_fields() -> None:
    assert SignupApprove.model_validate({}).trial_days is None
    assert SignupApprove.model_validate({"trial_days": 30}).trial_days == 30
    with pytest.raises(ValidationError):
        SignupApprove.model_validate({"tenant_id": str(uuid.uuid4())})


@pytest.mark.parametrize("reason", ["", "  ", "x" * 1001, "a\x00b"])
def test_reject_reason_bounds(reason: str) -> None:
    with pytest.raises(ValidationError):
        SignupReject.model_validate({"reason": reason})


def test_me_out_platform_admin_defaults_false() -> None:
    me = MeOut.model_validate(
        {
            "user_id": str(uuid.uuid4()),
            "email": "a@b.example",
            "tenant_id": str(uuid.uuid4()),
            "role": "owner",
            "memberships": [],
        }
    )
    assert me.is_platform_admin is False


# ---- slugs ----


@pytest.mark.parametrize(
    ("brand", "slug"),
    [
        ("Kalakar Weaves", "kalakar-weaves"),
        ("  Café  Déjà Vu!! ", "cafe-deja-vu"),
        ("ACME & Sons (India) Pvt. Ltd.", "acme-sons-india-pvt-ltd"),
        ("--a--", "brand"),
        ("गुलमोहर", "brand"),
        ("Z", "brand"),
        ("x" * 80, "x" * 32),
        ("abcdefghij abcdefghij abcdefghij-xyz", "abcdefghij-abcdefghij-abcdefghij"),
    ],
)
def test_slug_base(brand: str, slug: str) -> None:
    assert svc.slug_base(brand) == slug
    assert TENANT_SLUG.fullmatch(slug)


def test_slug_base_never_ends_with_hyphen() -> None:
    assert svc.slug_base("a" * 31 + " b") == "a" * 31


def test_slug_candidates_suffixes_fit_the_check() -> None:
    tokens = iter(["0a1b", "ffff"])
    got = list(svc.slug_candidates("x" * 80, attempts=3, token=lambda: next(tokens)))
    assert got == ["x" * 32, "x" * 32 + "-0a1b", "x" * 32 + "-ffff"]
    assert all(TENANT_SLUG.fullmatch(s) for s in got)
    assert len(list(svc.slug_candidates("Kalakar"))) == svc.SLUG_ATTEMPTS


# ---- the decision table ----


@pytest.mark.parametrize(
    ("status", "decision", "outcome"),
    [
        ("pending", "approve", "apply"),
        ("pending", "reject", "apply"),
        ("approved", "approve", "same"),
        ("rejected", "reject", "same"),
        ("approved", "reject", "conflict"),
        ("rejected", "approve", "conflict"),
    ],
)
def test_decision_outcome(status: str, decision: Any, outcome: str) -> None:
    assert svc.decision_outcome(status, decision) == outcome


# ---- the registry ----

SIGNUP_OPS = ("signup_create", "signup_status")
ADMIN_OPS = (
    "admin_signup_requests_list",
    "admin_signup_requests_approve",
    "admin_signup_requests_reject",
)


@pytest.mark.parametrize("op_id", SIGNUP_OPS)
def test_signup_ops_public(op_id: str) -> None:
    op = OPS[op_id]
    assert op.public and op.action is None and op.policy == "public"
    assert cases(op) == {"success", "validation"}


def test_signup_status_declares_401() -> None:
    assert 401 in declared_responses(OPS["signup_status"])
    assert 401 not in declared_responses(OPS["signup_create"])


@pytest.mark.parametrize("op_id", ADMIN_OPS)
def test_admin_ops_platform_admin_only(op_id: str) -> None:
    op = OPS[op_id]
    assert op.platform_admin and op.action is None and not op.public
    assert op.roles == NO_ROLES
    assert op.policy == PLATFORM_ADMIN
    assert {"missing_auth", "invalid_token", "wrong_role", "wrong_tenant"} <= cases(op)
    responses = declared_responses(op)
    assert responses[403]["content"]["application/json"]["example"]["error"]["type"] == (
        ErrorCode.PERMISSION_DENIED.value
    )


@pytest.mark.parametrize("op_id", ADMIN_OPS[1:])
def test_admin_decisions_declare_404_and_409(op_id: str) -> None:
    codes = OPS[op_id].error_codes
    assert codes[404] == (ErrorCode.NOT_FOUND,)
    assert codes[409] == (ErrorCode.INVALID_STATUS_TRANSITION,)


def test_no_new_error_codes() -> None:
    # MINOR only (docs/RULES.md §11): the flow reuses existing codes.
    used = {c for op_id in (*SIGNUP_OPS, *ADMIN_OPS) for cs in OPS[op_id].error_codes.values()
            for c in cs}  # fmt: skip
    assert {c.value for c in used} <= {
        "VALIDATION_FAILED", "PAYLOAD_TOO_LARGE", "UNAUTHENTICATED", "PERMISSION_DENIED",
        "NOT_FOUND", "INVALID_STATUS_TRANSITION", "RATE_LIMITED",
    }  # fmt: skip


def test_operation_rejects_public_and_platform_admin() -> None:
    with pytest.raises(ValueError, match="not both"):
        replace(OPS["signup_create"], platform_admin=True)
    with pytest.raises(ValueError, match="no action"):
        replace(OPS["admin_signup_requests_list"], action=Action.TENANT_READ)


def test_admin_routes_carry_platform_policy(v1_app: FastAPI) -> None:
    from fastapi.routing import iter_route_contexts

    found = {
        c.operation_id: route_policy(c)
        for c in iter_route_contexts(list(v1_app.routes))
        if getattr(c, "operation_id", None) in (*ADMIN_OPS, *SIGNUP_OPS)
    }
    assert found == {
        **dict.fromkeys(ADMIN_OPS, PLATFORM_ADMIN),
        **dict.fromkeys(SIGNUP_OPS, "public"),
    }


# ---- the platform-admin dependency ----


def _principal() -> Principal:
    return Principal(uuid.uuid4(), uuid.uuid4(), "owner", "owner@acme.test", "jti-1")


def _admin_app(monkeypatch: pytest.MonkeyPatch, *, admin: bool) -> TestClient:
    from app.api.v1 import deps
    from app.services import signup as signup_service

    async def fake_flag(_: Principal) -> bool:
        return admin

    monkeypatch.setattr(signup_service, "is_platform_admin", fake_flag)
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/x", dependencies=[deps.require_platform_admin()])
    async def x() -> dict[str, str]:
        return {"ok": "yes"}

    principal = _principal()
    app.dependency_overrides[deps.get_current_principal] = lambda: principal
    return TestClient(app, raise_server_exceptions=False)


def test_platform_admin_dependency_lets_admin_through(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _admin_app(monkeypatch, admin=True).get("/x").json() == {"ok": "yes"}


def test_platform_admin_dependency_refuses_owner_403(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _admin_app(monkeypatch, admin=False).get("/x")
    assert response.status_code == 403
    assert response.json()["error"]["type"] == ErrorCode.PERMISSION_DENIED.value


def test_platform_admin_dependency_needs_a_principal() -> None:
    from app.api.v1 import deps

    app = FastAPI()
    install_error_handlers(app)

    @app.get("/x", dependencies=[deps.require_platform_admin()])
    async def x() -> dict[str, str]:  # pragma: no cover - never reached
        return {}

    assert TestClient(app).get("/x").status_code == 401


def test_require_platform_admin_is_cached_and_marked() -> None:
    from app.api.v1 import deps

    dep = deps.require_platform_admin()
    assert dep is deps.require_platform_admin()
    app = FastAPI()

    @app.get("/x", dependencies=[dep])
    async def x() -> None:  # pragma: no cover
        return None

    route = next(r for r in app.routes if getattr(r, "path", "") == "/x")
    assert route_policy(route) == PLATFORM_ADMIN
    _ = Depends  # the marker is a Depends like require()'s


# ---- the service over fakes ----


NOW = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


def _row(**changes: Any) -> SignupRow:
    base = SignupRow(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        email="founder@kalakar.example",
        brand_name="Kalakar Weaves",
        owner_name="Meera Iyer",
        phone=None,
        website=None,
        status="pending",
        reason=None,
        created_tenant_id=None,
        decided_at=None,
        created_at=NOW,
    )
    return replace(base, **changes)


@dataclass
class World:
    users: dict[str, UserRow] = field(default_factory=dict)
    inserted_users: list[dict[str, Any]] = field(default_factory=list)
    requests: dict[uuid.UUID, SignupRow] = field(default_factory=dict)
    gucs: list[dict[str, Any]] = field(default_factory=list)
    audits: list[dict[str, Any]] = field(default_factory=list)
    tenants: list[dict[str, Any]] = field(default_factory=list)
    taken: set[str] = field(default_factory=set)
    race: bool = False
    email_race: bool = False


def _install(monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    @asynccontextmanager
    async def session() -> AsyncIterator[object]:
        yield object()

    async def set_gucs(_: object, **kw: Any) -> None:
        world.gucs.append(kw)

    class Users:
        def __init__(self, _: object) -> None:
            pass

        async def by_email(self, email: str) -> UserRow | None:
            return world.users.get(email)

        async def insert_signup_user(self, **kw: Any) -> bool:
            if world.email_race:  # a concurrent first sign-up took the email
                return False
            world.inserted_users.append(kw)
            return True

        async def is_platform_admin(self, _: uuid.UUID) -> bool:
            return True

    class Requests:
        def __init__(self, _: object) -> None:
            pass

        async def latest_for(self, user_id: uuid.UUID, *, pending_only: bool = False) -> Any:
            rows = [
                r
                for r in world.requests.values()
                if r.user_id == user_id and (not pending_only or r.status == "pending")
            ]
            return max(rows, key=lambda r: r.created_at) if rows else None

        async def insert_pending(self, *, user_id: uuid.UUID, **kw: Any) -> uuid.UUID | None:
            if world.race:
                world.race = False
                raced = _row(user_id=user_id)
                world.requests[raced.id] = raced
                return None
            row = _row(user_id=user_id, **kw)
            world.requests[row.id] = row
            return row.id

        async def lock(self, request_id: uuid.UUID) -> SignupRow | None:
            return world.requests.get(request_id)

        async def create_tenant(self, **kw: Any) -> None:
            if kw["slug"] in world.taken:
                raise SlugTakenError("slug taken")
            world.tenants.append(kw)

        async def decide(self, request_id: uuid.UUID, **kw: Any) -> None:
            row = world.requests[request_id]
            world.requests[request_id] = replace(
                row,
                status=kw["status"],
                reason=kw["reason"],
                created_tenant_id=kw["created_tenant_id"],
                decided_at=NOW,
            )

    class Audit:
        def __init__(self, _: object) -> None:
            pass

        async def write(self, **kw: Any) -> None:
            world.audits.append(kw)

    monkeypatch.setattr(svc, "identity_session", session)
    monkeypatch.setattr(svc, "set_gucs", set_gucs)
    monkeypatch.setattr(svc, "UserRepository", Users)
    monkeypatch.setattr(svc, "SignupRepository", Requests)
    monkeypatch.setattr(svc, "AuditRepository", Audit)
    monkeypatch.setattr(svc, "hash_password", lambda p: f"hash:{p}")
    monkeypatch.setattr(svc, "verify_password", lambda p, h: h == f"hash:{p}")


async def _signup(**changes: Any) -> svc.SignupAccepted:
    kwargs: dict[str, Any] = {
        "email": "founder@kalakar.example",
        "password": "a-long-enough-pass",
        "brand_name": "Kalakar Weaves",
        "owner_name": "Meera Iyer",
        "phone": None,
        "website": None,
        "ip": "203.0.113.7",
    }
    return await svc.signup(**{**kwargs, **changes})


def _user(world: World, *, active: bool = True) -> UserRow:
    user = UserRow(uuid.uuid4(), "founder@kalakar.example", "hash:a-long-enough-pass", active, None)
    world.users[user.email] = user
    return user


async def test_signup_new_email_creates_user_and_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World()
    _install(monkeypatch, world)
    accepted = await _signup()
    assert accepted.status == "pending"
    [made] = world.inserted_users
    assert made["email"] == "founder@kalakar.example"
    assert made["password_hash"] == "hash:a-long-enough-pass"
    assert made["display_name"] == "Meera Iyer"
    assert "is_platform_admin" not in made
    assert world.requests[accepted.request_id].user_id == made["user_id"]
    assert world.gucs[0] == {"login_email": "founder@kalakar.example"}
    assert world.gucs[1] == {"user_id": made["user_id"]}
    [audit] = world.audits
    assert audit["action"] == "signup.requested" and audit["tenant_id"] is None
    assert audit["actor_type"] == "system" and audit["entity_id"] == made["user_id"]


async def test_signup_new_email_race_answers_a_blank_202(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(email_race=True)
    _install(monkeypatch, world)
    accepted = await _signup()
    assert accepted.status == "pending"
    assert not world.requests and not world.audits


async def test_signup_existing_email_wrong_password_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World()
    _install(monkeypatch, world)
    _user(world)
    accepted = await _signup(password="not-the-password!")
    assert accepted.status == "pending"
    assert accepted.request_id not in world.requests
    assert not world.requests and not world.inserted_users and not world.audits


async def test_signup_inactive_user_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World()
    _install(monkeypatch, world)
    _user(world, active=False)
    await _signup()
    assert not world.requests and not world.audits


async def test_signup_existing_user_right_password_new_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World()
    _install(monkeypatch, world)
    user = _user(world)
    accepted = await _signup()
    assert world.requests[accepted.request_id].user_id == user.id
    assert not world.inserted_users


async def test_signup_while_pending_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World()
    _install(monkeypatch, world)
    user = _user(world)
    first = await _signup()
    second = await _signup(brand_name="Another Name")
    assert second.request_id == first.request_id
    assert len(world.requests) == 1 and len(world.audits) == 1
    assert world.requests[first.request_id].user_id == user.id


async def test_signup_after_rejection_opens_a_new_request(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World()
    _install(monkeypatch, world)
    user = _user(world)
    old = _row(user_id=user.id, status="rejected", reason="no", decided_at=NOW)
    world.requests[old.id] = old
    accepted = await _signup()
    assert accepted.request_id != old.id
    assert world.requests[accepted.request_id].status == "pending"


async def test_signup_pending_race_answers_the_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(race=True)
    _install(monkeypatch, world)
    _user(world)
    accepted = await _signup()
    assert world.requests[accepted.request_id].status == "pending"
    assert not world.audits


async def test_signup_status_refusals_none(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World()
    _install(monkeypatch, world)
    assert await svc.signup_status("nobody@x.example", "whatever-pass") is None
    user = _user(world)
    assert await svc.signup_status(user.email, "wrong-password!!") is None
    assert await svc.signup_status(user.email, "a-long-enough-pass") is None  # no request
    row = _row(user_id=user.id)
    world.requests[row.id] = row
    assert await svc.signup_status(user.email, "a-long-enough-pass") == row


def _admin() -> Principal:
    return Principal(uuid.uuid4(), uuid.uuid4(), "owner", "ops@platform.test", "jti")


async def test_approve_creates_trial_tenant_and_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World()
    _install(monkeypatch, world)
    row = _row()
    world.requests[row.id] = row
    admin = _admin()
    out = await svc.approve(admin, row.id, trial_days=None)
    [tenant] = world.tenants
    assert tenant["name"] == "Kalakar Weaves" and tenant["slug"] == "kalakar-weaves"
    assert tenant["trial_days"] == svc.DEFAULT_TRIAL_DAYS == 14
    assert tenant["owner_id"] == row.user_id and tenant["staff_email"] == admin.email
    assert out.status == "approved" and out.created_tenant_id == tenant["tenant_id"]
    assert {"tenant_id": tenant["tenant_id"]} in world.gucs
    [audit] = world.audits
    assert audit["action"] == "signup.approved" and audit["actor_type"] == "staff"
    assert audit["actor_email"] == admin.email and audit["tenant_id"] == tenant["tenant_id"]


async def test_approve_retries_a_taken_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(taken={"kalakar-weaves"})
    _install(monkeypatch, world)
    row = _row()
    world.requests[row.id] = row
    await svc.approve(_admin(), row.id, trial_days=30)
    [tenant] = world.tenants
    assert re.fullmatch(r"kalakar-weaves-[0-9a-f]{4}", tenant["slug"])
    assert tenant["trial_days"] == 30


async def test_approve_twice_answers_the_same_row(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World()
    _install(monkeypatch, world)
    row = _row()
    world.requests[row.id] = row
    first = await svc.approve(_admin(), row.id, trial_days=None)
    second = await svc.approve(_admin(), row.id, trial_days=None)
    assert second == first
    assert len(world.tenants) == 1 and len(world.audits) == 1


async def test_approve_rejected_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World()
    _install(monkeypatch, world)
    row = _row(status="rejected", reason="no", decided_at=NOW)
    world.requests[row.id] = row
    with pytest.raises(ConflictError) as info:
        await svc.approve(_admin(), row.id, trial_days=None)
    assert info.value.code is ErrorCode.INVALID_STATUS_TRANSITION
    assert not world.tenants


async def test_decisions_unknown_id_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, World())
    with pytest.raises(NotFoundError):
        await svc.approve(_admin(), uuid.uuid4(), trial_days=None)
    with pytest.raises(NotFoundError):
        await svc.reject(_admin(), uuid.uuid4(), reason="x")


async def test_reject_stores_reason_and_audits(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World()
    _install(monkeypatch, world)
    row = _row()
    world.requests[row.id] = row
    admin = _admin()
    out = await svc.reject(admin, row.id, reason="Could not verify the brand.")
    assert out.status == "rejected" and out.reason == "Could not verify the brand."
    assert out.created_tenant_id is None and not world.tenants
    [audit] = world.audits
    assert audit["action"] == "signup.rejected" and audit["tenant_id"] is None
    assert audit["entity_id"] == row.user_id and audit["reason"] == out.reason
    again = await svc.reject(admin, row.id, reason="another reason")
    assert again.reason == "Could not verify the brand." and len(world.audits) == 1


async def test_reject_approved_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World()
    _install(monkeypatch, world)
    row = _row(status="approved", created_tenant_id=uuid.uuid4(), decided_at=NOW)
    world.requests[row.id] = row
    with pytest.raises(ConflictError):
        await svc.reject(_admin(), row.id, reason="late")


# ---- `user make-admin` ----


def test_cli_make_admin_args() -> None:
    from app.cli import build_parser

    args = build_parser().parse_args(
        ["user", "make-admin", "--email", " Ops@Platform.Test ", "--staff-email", "s@ops.test"]
    )
    assert (args.group, args.command, args.email) == ("user", "make-admin", "ops@platform.test")


@pytest.mark.parametrize(
    "argv",
    [
        ["user", "make-admin", "--email", "no-at-sign", "--staff-email", "s@ops.test"],
        ["user", "make-admin", "--email", "a@b.test"],
        ["user", "make-admin", "--staff-email", "s@ops.test"],
    ],
)
def test_cli_make_admin_bad_args_exit_2(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    from app.cli import main

    with pytest.raises(SystemExit) as info:
        main(argv)
    capsys.readouterr()
    assert info.value.code == 2


# ---- revision 0010 ----


def _revision() -> Any:
    path = VERSIONS / "0010_signup_requests.py"
    spec = importlib.util.spec_from_file_location("rev_0010", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0010_chain_and_shape() -> None:
    rev = _revision()
    assert rev.revision == "0010_signup_requests"
    assert rev.down_revision == "0009_analytics_trace"
    ddl = "\n".join((*rev.USERS_UP, *rev.TABLE_UP))
    assert "is_platform_admin boolean NOT NULL DEFAULT false" in ddl
    assert (
        "uq_signup_requests_pending ON public.signup_requests (user_id) WHERE status = 'pending'"
        in ddl
    )
    assert "tenant_id uuid" not in ddl.split("CREATE TABLE public.signup_requests")[1].replace(
        "created_tenant_id uuid", ""
    )
    assert "SECURITY DEFINER" in ddl and "REVOKE ALL ON FUNCTION" in ddl
    assert "REVOKE DELETE, TRUNCATE ON public.signup_requests FROM app_rw" in ddl
    names = [name for _, name, _ in rev.POLICIES]
    assert names == [
        "sr_self_select", "sr_self_insert", "sr_admin", "t_platform", "t_migrator",
        "users_platform_admin",
    ]  # fmt: skip
    readable = rev.SIGNUP_READABLE
    assert "phone" not in readable and "owner_name" not in readable


def test_0010_is_the_single_head() -> None:
    parents = set()
    revisions = set()
    for path in VERSIONS.glob("0*.py"):
        text = path.read_text("utf-8")
        revisions.add(re.search(r'^revision: str = "([^"]+)"', text, re.M).group(1))  # type: ignore[union-attr]
        down = re.search(r'^down_revision: .*= "([^"]+)"', text, re.M)
        if down:
            parents.add(down.group(1))
    assert revisions - parents == {"0010_signup_requests"}
