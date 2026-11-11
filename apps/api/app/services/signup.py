"""Self-serve brand sign-up with platform-admin approval (ADR-0019).

- `signup()`: the public form. The DPP 1.4.1 password rules run first, before any lookup, so
  a 422 (`PolicyError`) says nothing about the account. A new email gets a global `users` row
  (argon2id) and a pending `signup_requests` row; an existing email with the right password gets
  a pending request too (or keeps its current one: re-submitting is idempotent). For an existing
  email the 202 always carries a fresh random id, whatever the password (DPP-1: a stable id was a
  password oracle); the applicant reads the real request through `signup_status()`. An existing
  email takes the per-user lockout gate: a wrong password counts, and a locked account writes
  nothing. Each path costs one argon2 operation.
- `signup_status()`: the applicant's latest request, behind email and password (None for any
  refusal: the caller answers 401 like the login). It takes the lockout gate: a wrong password
  counts, a locked account answers None whatever the password, and a success does not clear the
  counter. A pending applicant has no membership, so the login keeps answering 401 and no token,
  tenant or tenant data exists for them.
- Status handles (ADR-0019 amendment, 11 Nov 2026): every answer that observes a pending request
  (the form, the status read, the Google `pending` outcome and the Google sign-up) carries a
  fresh opaque handle, 32 random bytes in base64url. Only its sha256 is stored on the request,
  with a 30-day expiry set by the database (`app_set_signup_status_handle`, only for the
  caller's own pending request); a newer handle replaces the older one. `poll_status()` reads
  the one request a live handle names, and only its status, brand name, request and decision
  times and the reject reason. A blank answer of the form (an existing email, a race) carries a
  random handle that names nothing, so its shape never differs.
- `is_platform_admin()`, `list_requests()`, `approve()`, `reject()`: the platform admin's queue.
  The admin is a normal principal (a member of some tenant, e.g. the staff tenant) whose own
  `users.is_platform_admin` is true; RLS on `signup_requests` checks the same flag
  (`app_is_platform_admin()`), so a non-admin reads and changes nothing even past the router.
  Approve creates the tenant (`trial`, `trial_days` default 30, ADR-0029) and its owner membership through
  the staff CLI's own statements, and audits as `staff` with the admin's email. Approving an
  approved request, or rejecting a rejected one, answers the stored row; the other crossing is
  409 `INVALID_STATUS_TRANSITION`. Client item 1: approve also copies the applicant's website (both
  sign-up paths store it on the request) to the new brand's `brand_profiles.website_url`; a
  website that does not fit the column is left out, never blocking the approval.
- `ask()` and `reply()` (client item 2, ADR-0019 amendment): instead of deciding, a platform admin
  may ask the applicant one question; the request stays `pending` (no new status, so every
  reader of the status is unchanged). The applicant reads it through `poll_status()` and sends one
  reply authorised by the same status handle (`app_reply_signup_info`, the only writer of the
  reply). Asking the same question again, or sending the same reply again, answers the stored
  row; a second, different one, or either on a decided request, is 409
  `INVALID_STATUS_TRANSITION`. Both are audited (`signup.info_requested` as `staff` with the
  admin's email and the question as the reason; `signup.info_replied` as `system`, without the
  reply's text). The admin may still approve or reject at any time.
"""

from __future__ import annotations

import hashlib
import secrets
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal

from mkos_schemas.codes import ErrorCode

from app.core import password_policy
from app.core.errors import ConflictError, NotFoundError
from app.core.passwords import hash_password, off_loop, verify_password
from app.core.rbac import Principal
from app.core.website import profile_website
from app.repositories.identity import (
    AuditRepository,
    AuthState,
    SignupPollRow,
    SignupRepository,
    SignupRow,
    SlugTakenError,
    UserRepository,
    UserRow,
)
from app.repositories.parties import BrandProfileRepository
from app.repositories.transactions import identity_session, set_gucs
from app.services import auth_events
from app.services.auth import dummy_hash, inet
from app.services.sign_in_policy import is_locked

DEFAULT_TRIAL_DAYS: Final = 30  # ADR-0029 (was 14, ADR-0019)
SLUG_BASE_MAX: Final = 32  # leaves room for "-xxxx" within ck_tenants_slug's 40
SLUG_ATTEMPTS: Final = 8
SLUG_FALLBACK: Final = "brand"
STATUS_HANDLE_BYTES: Final = 32  # 43 base64url characters
Decision = Literal["approve", "reject"]

Outcome = Literal["apply", "same", "conflict"]

__all__ = [
    "DEFAULT_TRIAL_DAYS",
    "SignupAccepted",
    "SignupPollRow",
    "SignupRow",
    "StatusRead",
    "approve",
    "ask",
    "ask_outcome",
    "decision_outcome",
    "is_platform_admin",
    "issue_status_handle",
    "list_requests",
    "new_status_handle",
    "poll_status",
    "reject",
    "reply",
    "signup",
    "signup_status",
    "slug_base",
    "slug_candidates",
    "status_handle_hash",
    "verify_existing_user",
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


def ask_outcome(status: str, current: str | None, question: str) -> Outcome:
    """What asking `question` does to a request: one question per pending request; the same
    question again answers the stored row; any other crossing is a conflict."""
    if status != "pending":
        return "conflict"
    if current is None:
        return "apply"
    return "same" if current == question else "conflict"


def new_status_handle() -> str:
    """A fresh opaque status handle: 32 random bytes, base64url without padding."""
    return secrets.token_urlsafe(STATUS_HANDLE_BYTES)


def status_handle_hash(raw: str) -> bytes:
    """The only form of a status handle the database keeps or is asked about."""
    return hashlib.sha256(raw.encode("ascii")).digest()


# ---- status handles ----


async def issue_status_handle(session: Any, request_id: uuid.UUID) -> str | None:
    """A new handle for the caller's own pending request (`app.user_id` set), replacing any
    older one; None when the request is not the caller's or not pending."""
    raw = new_status_handle()
    if await SignupRepository(session).set_status_handle(request_id, status_handle_hash(raw)):
        return raw
    return None


async def poll_status(raw: str) -> SignupPollRow | None:
    """The request a live handle names; None for an unknown or expired handle (one answer)."""
    async with identity_session() as session:
        return await SignupRepository(session).by_status_handle(status_handle_hash(raw))


# ---- the public form ----


@dataclass(frozen=True, slots=True)
class SignupAccepted:
    request_id: uuid.UUID
    status: Literal["pending"] = "pending"
    # a blank answer gets a random handle that names nothing: the same shape every time
    status_handle: str = field(default_factory=new_status_handle)


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
    """Raises `password_policy.PolicyError` (a 422) for a password that breaks the DPP rules,
    before any lookup."""
    breaches = password_policy.check(password, email=email, display_name=owner_name)
    if breaches:
        raise password_policy.PolicyError(breaches)
    async with identity_session() as session:
        await set_gucs(session, login_email=email)
        users = UserRepository(session)
        requests = SignupRepository(session)
        if not await users.lock_login_email(email):  # every email queues the same way
            await off_loop(verify_password, password, dummy_hash())
            return SignupAccepted(uuid.uuid4())
        user = await users.by_email(email)
        if user is None:
            user_id = uuid.uuid4()
            created = await users.insert_signup_user(
                user_id=user_id,
                email=email,
                password_hash=await off_loop(hash_password, password),
                display_name=owner_name,
            )
            if not created:  # a concurrent first sign-up of this email won: the same blank 202
                return SignupAccepted(uuid.uuid4())
            await set_gucs(session, user_id=user_id)
        else:
            # every answer for an existing email is a fresh id (right, wrong or locked password)
            if await verify_existing_user(session, user, password, ip=ip):
                await set_gucs(session, user_id=user.id)
                pending = await requests.latest_for(user.id, pending_only=True)
                pending_id = (
                    pending.id
                    if pending is not None
                    else await _request(
                        session, user.id, brand_name, owner_name, phone, website, ip
                    )
                )
                handle = await issue_status_handle(session, pending_id)
                if handle is not None:
                    return SignupAccepted(uuid.uuid4(), status_handle=handle)
            return SignupAccepted(uuid.uuid4())
        request_id = await _request(session, user_id, brand_name, owner_name, phone, website, ip)
        handle = await issue_status_handle(session, request_id)
    if handle is None:  # pragma: no cover - the request was just written for this user
        raise RuntimeError("no status handle for a new pending sign-up")
    return SignupAccepted(request_id, status_handle=handle)


async def verify_existing_user(
    session: Any, user: UserRow, password: str, *, ip: str | None
) -> bool:
    """The lockout gate, then one argon2 verify (always, for timing parity). True only for the
    right password of an active, unlocked user; a wrong password counts a failure. Also the
    invite accept for an account that cannot sign in yet (`invites.accept_password`)."""
    users = UserRepository(session)
    await set_gucs(session, user_id=user.id)
    state = await users.begin_auth_attempt(user.id)
    stored = user.hashed_password
    verified = await off_loop(verify_password, password, stored or dummy_hash())
    if not isinstance(state, AuthState) or is_locked(state.locked_until, datetime.now(UTC)):
        return False
    if not verified or stored is None:
        if await users.note_auth_failure(user.id):
            await auth_events.record(
                session, action="auth.locked", user_id=user.id, tenant_id=None, ip=inet(ip)
            )
        return False
    return user.is_active


async def _request(
    session: Any,
    user_id: uuid.UUID,
    brand_name: str,
    owner_name: str,
    phone: str | None,
    website: str | None,
    ip: str | None,
) -> uuid.UUID:
    """A pending request (or the one a concurrent sign-up of the same user just stored)."""
    requests = SignupRepository(session)
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
        return raced.id
    await AuditRepository(session).write(
        tenant_id=None,
        actor_type="system",
        actor_user_id=None,
        action="signup.requested",
        entity_type="user",
        entity_id=user_id,
        ip=inet(ip),
    )
    return request_id


@dataclass(frozen=True, slots=True)
class StatusRead:
    """The applicant's newest request, and a new status handle while it is pending."""

    row: SignupRow
    status_handle: str | None = None


async def signup_status(email: str, password: str, *, ip: str | None = None) -> StatusRead | None:
    """The applicant's newest request (with a new status handle while pending); None for an
    unknown email, a wrong password, a locked account, an inactive user or no request (one
    argon2 verify each)."""
    async with identity_session() as session:
        await set_gucs(session, login_email=email)
        users = UserRepository(session)
        if not await users.lock_login_email(email):  # every email queues the same way
            await off_loop(verify_password, password, dummy_hash())
            return None
        user = await users.by_email(email)
        if user is None:
            await off_loop(verify_password, password, dummy_hash())
            return None
        if not await verify_existing_user(session, user, password, ip=ip):
            return None
        row = await SignupRepository(session).latest_for(user.id)
        if row is None:
            return None
        handle = await issue_status_handle(session, row.id) if row.status == "pending" else None
        return StatusRead(row, handle)


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


async def _copy_website(session: Any, tenant_id: uuid.UUID, website: str | None) -> None:
    """Client item 1: the applicant's website onto the new brand's profile row (created the way
    `PATCH /brand-profile` creates it). No website writes
    nothing. The session's tenant GUC is already the new tenant."""
    url = profile_website(website)
    if url is None:
        return
    profiles = BrandProfileRepository(session, tenant_id)
    await profiles.ensure()
    await profiles.set_website(url)


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
        await _copy_website(session, tenant_id, row.website)
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


# ---- one question and one reply (client item 2) ----


async def ask(principal: Principal, request_id: uuid.UUID, *, question: str) -> SignupRow:
    async with identity_session() as session:
        await set_gucs(session, user_id=principal.user_id)
        requests = SignupRepository(session)
        row = await requests.lock(request_id)
        if row is None:
            raise NotFoundError("signup_request")
        outcome = ask_outcome(row.status, row.info_question, question)
        if outcome == "same":
            return row
        if outcome == "conflict":
            raise _conflict()
        await requests.ask(request_id, question=question)
        await AuditRepository(session).write(
            tenant_id=None,
            actor_type="staff",
            actor_user_id=None,
            action="signup.info_requested",
            entity_type="user",
            entity_id=row.user_id,
            reason=question,
            actor_email=principal.email,
        )
        asked = await requests.lock(request_id)
    if asked is None:
        raise RuntimeError("an asked sign-up is not readable")
    return asked


async def reply(raw: str, text: str, *, ip: str | None = None) -> SignupPollRow | None:
    """The applicant's one reply on the request a live handle names; None for an unknown or
    expired handle (the poll's one 404). Raises the 409 for no open question, a decided request
    or a second, different reply."""
    digest = status_handle_hash(raw)
    async with identity_session() as session:
        requests = SignupRepository(session)
        outcome = await requests.reply_info(digest, text)
        if outcome == "not_found":
            return None
        if outcome == "conflict":
            raise _conflict()
        if outcome == "replied":
            user_id = await requests.handle_user_id(digest)
            if user_id is None:  # pragma: no cover - the handle was live a statement ago
                raise RuntimeError("a replied sign-up has no applicant")
            await AuditRepository(session).write(
                tenant_id=None,
                actor_type="system",
                actor_user_id=None,
                action="signup.info_replied",
                entity_type="user",
                entity_id=user_id,
                ip=inet(ip),
            )
        return await requests.by_status_handle(digest)
