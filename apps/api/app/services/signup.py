"""Self-serve brand sign-up with platform-admin approval (ADR-0019).

- `signup()`: the public form. A new email gets a global `users` row (argon2id) and a pending
  `signup_requests` row; an existing email with the right password gets a pending request too
  (or its current one: re-submitting is idempotent); an existing email with a wrong password
  gets the same 202 with a fresh id and nothing is written, so the answer never reveals whether
  the email had an account. Each path costs one argon2 operation.
- `signup_status()`: the applicant's latest request, behind email and password (None for any
  refusal: the caller answers 401 like the login). A pending applicant has no membership, so the
  login keeps answering 401 and no token, tenant or tenant data exists for them.
- `is_platform_admin()`, `list_requests()`, `approve()`, `reject()`: the platform admin's queue.
  The admin is a normal principal (a member of some tenant, e.g. the staff tenant) whose own
  `users.is_platform_admin` is true; RLS on `signup_requests` checks the same flag
  (`app_is_platform_admin()`), so a non-admin reads and changes nothing even past the router.
  Approve creates the tenant (`trial`, `trial_days` default 14) and its owner membership through
  the staff CLI's own statements, and audits as `staff` with the admin's email. Approving an
  approved request, or rejecting a rejected one, answers the stored row; the other crossing is
  409 `INVALID_STATUS_TRANSITION`.
"""

from __future__ import annotations

import secrets
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from mkos_schemas.codes import ErrorCode

from app.core.errors import ConflictError, NotFoundError
from app.core.passwords import hash_password, verify_password
from app.core.rbac import Principal
from app.repositories.identity import (
    AuditRepository,
    SignupRepository,
    SignupRow,
    SlugTakenError,
    UserRepository,
)
from app.repositories.transactions import identity_session, set_gucs
from app.services.auth import dummy_hash, inet

DEFAULT_TRIAL_DAYS: Final = 14
SLUG_BASE_MAX: Final = 32  # leaves room for "-xxxx" within ck_tenants_slug's 40
SLUG_ATTEMPTS: Final = 8
SLUG_FALLBACK: Final = "brand"
Decision = Literal["approve", "reject"]
Outcome = Literal["apply", "same", "conflict"]

__all__ = [
    "DEFAULT_TRIAL_DAYS",
    "SignupAccepted",
    "SignupRow",
    "approve",
    "decision_outcome",
    "is_platform_admin",
    "list_requests",
    "reject",
    "signup",
    "signup_status",
    "slug_base",
    "slug_candidates",
]


# ---- pure helpers ----


def slug_base(brand_name: str) -> str:
    """A `ck_tenants_slug` stem from the brand name: ASCII-folded, lower-case, runs of anything
    else as one hyphen, at most 32 characters, never shorter than 2 (`brand` then)."""
    folded = unicodedata.normalize("NFKD", brand_name).encode("ascii", "ignore").decode("ascii")
    out: list[str] = []
    for ch in folded.lower():
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    stem = "".join(out)[:SLUG_BASE_MAX].strip("-")
    return stem if len(stem) >= 2 else SLUG_FALLBACK


def slug_candidates(
    brand_name: str,
    *,
    attempts: int = SLUG_ATTEMPTS,
    token: Callable[[], str] = lambda: secrets.token_hex(2),
) -> Iterator[str]:
    """The stem, then the stem with a random 4-hex suffix, `attempts` slugs in all."""
    base = slug_base(brand_name)
    yield base
    for _ in range(attempts - 1):
        yield f"{base}-{token()}"


def decision_outcome(status: str, decision: Decision) -> Outcome:
    """What a decision does to a request in `status`: a pending request is decided; the same
    decision again answers the stored row (idempotent); the opposite one is a conflict."""
    if status == "pending":
        return "apply"
    if (status, decision) in (("approved", "approve"), ("rejected", "reject")):
        return "same"
    return "conflict"


# ---- the public form ----


@dataclass(frozen=True, slots=True)
class SignupAccepted:
    request_id: uuid.UUID
    status: Literal["pending"] = "pending"


async def signup(
    *,
    email: str,
    password: str,
    brand_name: str,
    owner_name: str,
    phone: str | None,
    website: str | None,
    ip: str | None,
) -> SignupAccepted:
    async with identity_session() as session:
        await set_gucs(session, login_email=email)
        users = UserRepository(session)
        requests = SignupRepository(session)
        user = await users.by_email(email)
        if user is None:
            user_id = uuid.uuid4()
            await users.insert_signup_user(
                user_id=user_id,
                email=email,
                password_hash=hash_password(password),
                display_name=owner_name,
            )
            await set_gucs(session, user_id=user_id)
        else:
            stored = user.hashed_password
            verified = verify_password(password, stored or dummy_hash())
            if not (verified and stored is not None and user.is_active):
                return SignupAccepted(uuid.uuid4())  # names nothing; nothing written
            user_id = user.id
            await set_gucs(session, user_id=user_id)
            current = await requests.latest_for(user_id, pending_only=True)
            if current is not None:
                return SignupAccepted(current.id)
        request_id = await requests.insert_pending(
            user_id=user_id,
            brand_name=brand_name,
            owner_name=owner_name,
            phone=phone,
            website=website,
        )
        if request_id is None:  # a concurrent sign-up of the same user won the index
            raced = await requests.latest_for(user_id, pending_only=True)
            if raced is None:
                raise RuntimeError("no pending sign-up after a pending-index conflict")
            return SignupAccepted(raced.id)
        await AuditRepository(session).write(
            tenant_id=None,
            actor_type="system",
            actor_user_id=None,
            action="signup.requested",
            entity_type="user",
            entity_id=user_id,
            ip=inet(ip),
        )
    return SignupAccepted(request_id)


async def signup_status(email: str, password: str) -> SignupRow | None:
    """The applicant's newest request; None for an unknown email, a wrong password, an
    inactive user or no request (one argon2 verify each)."""
    async with identity_session() as session:
        await set_gucs(session, login_email=email)
        user = await UserRepository(session).by_email(email)
        stored = user.hashed_password if user is not None and user.hashed_password else None
        verified = verify_password(password, stored or dummy_hash())
        if user is None or stored is None or not verified or not user.is_active:
            return None
        await set_gucs(session, user_id=user.id)
        return await SignupRepository(session).latest_for(user.id)


# ---- the platform admin's queue ----


async def is_platform_admin(principal: Principal) -> bool:
    async with identity_session() as session:
        await set_gucs(session, user_id=principal.user_id)
        return await UserRepository(session).is_platform_admin(principal.user_id)


@dataclass(frozen=True, slots=True)
class RequestPage:
    items: list[SignupRow]
    has_more: bool


async def list_requests(
    principal: Principal,
    *,
    status: str | None,
    limit: int,
    after: tuple[datetime, uuid.UUID] | None,
) -> RequestPage:
    async with identity_session() as session:
        await set_gucs(session, user_id=principal.user_id)
        rows = await SignupRepository(session).page(status=status, limit=limit, after=after)
    return RequestPage(rows[:limit], len(rows) > limit)


def _conflict() -> ConflictError:
    return ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "signup_requests_status")


async def approve(
    principal: Principal, request_id: uuid.UUID, *, trial_days: int | None
) -> SignupRow:
    async with identity_session() as session:
        await set_gucs(session, user_id=principal.user_id)
        requests = SignupRepository(session)
        row = await requests.lock(request_id)
        if row is None:
            raise NotFoundError("signup_request")
        outcome = decision_outcome(row.status, "approve")
        if outcome == "same":
            return row
        if outcome == "conflict":
            raise _conflict()
        tenant_id = uuid.uuid4()
        await set_gucs(session, tenant_id=tenant_id)
        for slug in slug_candidates(row.brand_name):
            try:
                await requests.create_tenant(
                    tenant_id=tenant_id,
                    name=row.brand_name,
                    slug=slug,
                    trial_days=trial_days or DEFAULT_TRIAL_DAYS,
                    owner_id=row.user_id,
                    staff_email=principal.email,
                )
                break
            except SlugTakenError:
                continue
        else:
            raise RuntimeError("no free tenant slug for the brand name")
        await requests.decide(
            request_id,
            status="approved",
            reason=None,
            decided_by=principal.user_id,
            created_tenant_id=tenant_id,
        )
        await AuditRepository(session).write(
            tenant_id=tenant_id,
            actor_type="staff",
            actor_user_id=None,
            action="signup.approved",
            entity_type="tenant",
            entity_id=tenant_id,
            actor_email=principal.email,
        )
        decided = await requests.lock(request_id)
    if decided is None:
        raise RuntimeError("an approved sign-up is not readable")
    return decided


async def reject(principal: Principal, request_id: uuid.UUID, *, reason: str) -> SignupRow:
    async with identity_session() as session:
        await set_gucs(session, user_id=principal.user_id)
        requests = SignupRepository(session)
        row = await requests.lock(request_id)
        if row is None:
            raise NotFoundError("signup_request")
        outcome = decision_outcome(row.status, "reject")
        if outcome == "same":
            return row
        if outcome == "conflict":
            raise _conflict()
        await requests.decide(
            request_id,
            status="rejected",
            reason=reason,
            decided_by=principal.user_id,
            created_tenant_id=None,
        )
        await AuditRepository(session).write(
            tenant_id=None,
            actor_type="staff",
            actor_user_id=None,
            action="signup.rejected",
            entity_type="user",
            entity_id=row.user_id,
            reason=reason,
            actor_email=principal.email,
        )
        decided = await requests.lock(request_id)
    if decided is None:
        raise RuntimeError("a rejected sign-up is not readable")
    return decided
