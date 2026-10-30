"""Sign in and sign up with Google (ADR-0020), always followed by Kuinn's own login code.

The browser keeps a random binding secret in sessionStorage and the return path; the server takes
no redirect parameter and sets no cookie. One start endpoint serves the login and the sign-up
pages alike: the browser remembers which page it came from only to choose its copy.

1. `start(b)`: a `login_flows` row (`authorize`, 10 minutes) holding the PKCE verifier and the
   nonce's sha256, bound to `b` (the binding's sha256), keyed by the sha256 of a fresh `state`;
   the answer is Google's consent URL.
2. `callback(code, state, error)`: consumes the authorize row, exchanges the code (PKCE), verifies
   the ID token (`app.core.google_oidc`), refuses an identity Google does not own (one generic
   failure, audited), and writes a `handle` row (60 seconds) with the identity under the same
   binding. The answer is the dashboard's `/auth/google?handle=` (or `?error=`).
3. `complete(handle, binding)`: consumes the handle only with the right binding (the OWASP
   login-CSRF check), then one transaction reads the user and applies `decide()`:
   - `sign_in`: the shared `_sign_in()` with `fed` proven (tokens, or under `all` the code or
     enrolment challenge); a locked or busy account is the generic failure, uncounted.
   - `pending`: the user's latest sign-up request (Google proved the email).
   - `needs_details`: no account yet; a `details` row (15 minutes) and the prefill (owner name
     from Google, a brand name suggested from a company email domain).
   - `link_required`: an existing account without Google; a `link` row (15 minutes). The user
     proves the account with their Kuinn password (`link()`), then their code, and only then is
     the Google account linked (`_sign_in`'s `link_sub`).
   - anything else: the one generic failure (no account oracle), audited with its reason.
4. `link(link_handle, binding, password)`: the password sign-in with the link riding on it.
5. `signup_details(details_handle, binding, form)`: a new user with no password, Google's `sub`
   and a verified email, and a pending sign-up request marked `verified_via = google`. A platform
   admin still approves every brand (ADR-0019).

Nothing here logs an email, a name, a code, a token or a handle. Audit rows carry ids and reason
codes only.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from urllib.parse import urlencode
from uuid import UUID

from structlog import get_logger

from app.adapters.oauth import OAuthError
from app.adapters.oauth.google import exchange_code, fetch_jwks
from app.core import google_oidc
from app.core.config import Settings, get_settings
from app.repositories.identity import (
    AuditRepository,
    MembershipRepository,
    SignupRepository,
    SignupRow,
    UserRepository,
    UserRow,
)
from app.repositories.login_flows import FlowIdentity, LoginFlowRepository
from app.repositories.transactions import identity_session, set_gucs
from app.services import auth, auth_events

log = get_logger(__name__)

AUTHORIZE_TTL_SECONDS: Final = 600
HANDLE_TTL_SECONDS: Final = 60
DETAILS_TTL_SECONDS: Final = 900
LINK_TTL_SECONDS: Final = 900
UNAVAILABLE_LOCATION: Final = "/login?error=google_unavailable"
APP_ROUTE: Final = "/auth/google"

Outcome = Literal["sign_in", "pending", "needs_details", "link_required"]


class GoogleDisabled(Exception):  # noqa: N818 - a control-flow signal for the 501
    """`GOOGLE_OAUTH_ENABLED=false`."""


class FlowInvalid(Exception):  # noqa: N818 - a refusal: 401 OAUTH_STATE_INVALID
    """An unknown, used or expired handle, or a binding that is not this browser's."""


@dataclass(frozen=True, slots=True)
class Failure:
    """The one generic failure (401 UNAUTHENTICATED); `reason` is audited, never shown."""

    reason: str


# ---- the pure rules ----

# Mailbox providers whose domain names nobody's brand (D3): no brand name is suggested for them.
CONSUMER_DOMAINS: Final = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "outlook.in",
        "hotmail.com",
        "hotmail.co.in",
        "live.com",
        "live.in",
        "msn.com",
        "yahoo.com",
        "yahoo.co.in",
        "yahoo.in",
        "ymail.com",
        "rocketmail.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "pm.me",
        "rediffmail.com",
        "rediff.com",
        "zoho.com",
        "zohomail.com",
        "zohomail.in",
        "gmx.com",
        "gmx.net",
        "mail.com",
        "yandex.com",
        "yandex.ru",
        "tutanota.com",
        "fastmail.com",
        "hey.com",
        "inbox.com",
        "qq.com",
        "163.com",
        "126.com",
        "naver.com",
    }
)
# Second-level public suffixes: `acme.co.in` names `acme`, not `co`.
SECOND_LEVEL_SUFFIXES: Final = frozenset(
    {
        "co.in", "net.in", "org.in", "firm.in", "gen.in", "ind.in", "ac.in", "edu.in",
        "co.uk", "org.uk", "ltd.uk", "plc.uk", "com.au", "net.au", "org.au", "co.nz",
        "com.sg", "com.my", "co.za", "com.br", "co.jp", "com.cn", "com.hk", "co.id",
        "com.ph", "com.pk", "com.bd", "com.np", "com.lk", "ae.org", "com.mx", "com.tr",
    }
)  # fmt: skip
_LABEL: Final = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
BRAND_MAX: Final = 200


def suggest_brand(email: str) -> str | None:
    """A brand name from a company email's domain (D3): the registrable label, title-cased, split
    only at hyphens and underscores (`acme-naturals.co.in` -> `Acme Naturals`,
    `acmenaturals.com` -> `Acmenaturals`); never for a mailbox provider, a digits-only or one-letter
    label, or an IDN. The user always edits it."""
    if email.count("@") != 1:
        return None
    domain = email.rsplit("@", 1)[1].lower().rstrip(".")
    if domain in CONSUMER_DOMAINS or not domain.isascii():
        return None
    labels = domain.split(".")
    if len(labels) < 2:
        return None
    tail = ".".join(labels[-2:])
    index = -3 if tail in SECOND_LEVEL_SUFFIXES and len(labels) >= 3 else -2
    label = labels[index]
    if label.startswith("xn--") or not _LABEL.fullmatch(label) or label.isdigit():
        return None
    words = [w for w in re.split(r"[-_]", label) if w]
    if len(label) < 2 or not words:
        return None
    return " ".join(w.capitalize() for w in words)[:BRAND_MAX]


@dataclass(frozen=True, slots=True)
class Situation:
    """What `complete` knows about the Google identity's account, for `decide()`."""

    by_sub: bool  # the user was found by the linked Google account (else by email, or none)
    exists: bool
    is_platform_admin: bool = False
    is_active: bool = True
    has_password: bool = True
    linked_to_other: bool = False  # found by email, but linked to a different Google account
    has_membership: bool = False
    has_request: bool = False


@dataclass(frozen=True, slots=True)
class Decision:
    outcome: Outcome | None
    reason: str | None = None  # the audited reason of a failure (outcome None)


def decide(s: Situation) -> Decision:
    """The decision table (plan §4, as changed by D1 and D4), checked in order. Every refusal is
    the same generic failure to the browser; only the audit row says which."""
    if not s.exists:
        return Decision("needs_details")
    if s.is_platform_admin:
        return Decision(None, "google_admin")
    if not s.is_active:
        return Decision(None, "inactive_user")
    if s.by_sub:
        if s.has_membership:
            return Decision("sign_in")
        if s.has_request:
            return Decision("pending")
        return Decision(None, "no_active_membership")
    if s.linked_to_other:
        return Decision(None, "google_link_refused")
    if s.has_membership:
        if s.has_password:
            return Decision("link_required")
        return Decision(None, "google_link_refused")
    if s.has_request:
        return Decision("pending")
    return Decision(None, "google_unvetted_conflict")


# ---- results ----


@dataclass(frozen=True, slots=True)
class Details:
    email: str = field(repr=False)
    owner_name: str | None = field(repr=False)
    given_name: str | None = field(repr=False)
    family_name: str | None = field(repr=False)
    suggested_brand_name: str | None = field(repr=False)
    details_handle: str = field(repr=False)
    details_expires_in: int = DETAILS_TTL_SECONDS


@dataclass(frozen=True, slots=True)
class Link:
    email: str = field(repr=False)
    link_handle: str = field(repr=False)
    link_expires_in: int = LINK_TTL_SECONDS


@dataclass(frozen=True, slots=True)
class Completed:
    outcome: Outcome
    login: auth.SignInResult | None = None
    signup: SignupRow | None = None
    details: Details | None = None
    link: Link | None = None


# ---- the JWKS cache (one per process) ----

_JWKS: Final = google_oidc.JwksCache()
_JWKS_LOCK: Final = asyncio.Lock()


async def _keys(settings: Settings, *, refetch: bool = False) -> dict[str, Any]:
    async with _JWKS_LOCK:
        if refetch or not _JWKS.fresh():
            _JWKS.store(google_oidc.jwks_from_bytes(await fetch_jwks(settings.google_jwks_url)))
        return dict(_JWKS.keys)


async def _verify(settings: Settings, id_token: str, nonce_hash: bytes) -> google_oidc.Identity:
    client_id = settings.google_oauth_client_id or ""
    try:
        return google_oidc.verify_id_token(
            id_token,
            await _keys(settings),
            client_id=client_id,
            issuer=settings.google_issuer,
            nonce_hash=nonce_hash,
        )
    except google_oidc.UnknownKey:
        if not _JWKS.may_refetch_for_miss():
            raise
        return google_oidc.verify_id_token(
            id_token,
            await _keys(settings, refetch=True),
            client_id=client_id,
            issuer=settings.google_issuer,
            nonce_hash=nonce_hash,
        )


# ---- helpers ----


def _enabled() -> Settings:
    settings = get_settings()
    if not settings.google_oauth_enabled:
        raise GoogleDisabled
    return settings


def _digest(value: str) -> bytes:
    return google_oidc.sha256(value)


def _binding(secret: str) -> bytes:
    return _digest(secret)


async def _failed(session: Any, user_id: UUID | None, reason: str, ip: str | None) -> None:
    await auth_events.record(
        session,
        action="auth.login_failed",
        user_id=user_id,
        tenant_id=None,
        ip=auth.inet(ip),
        reason=reason,
        actor_type="system",
    )


def app_location(settings: Settings, **query: str) -> str:
    """`{DASHBOARD_BASE_URL}/auth/google?…`: the app route that finishes the sign-in."""
    return f"{settings.dashboard_base_url}{APP_ROUTE}?{urlencode(query)}"


# ---- 1. start ----


async def start(b: str) -> str:
    """Google's consent URL for a browser whose binding's sha256 is `b`. Raises `GoogleDisabled`
    with the flag off, `FlowInvalid` for a malformed `b`."""
    settings = _enabled()
    binding_hash = google_oidc.binding_hash(b)
    if binding_hash is None:
        raise FlowInvalid
    state, nonce, verifier = (
        google_oidc.new_token(),
        google_oidc.new_token(),
        google_oidc.new_verifier(),
    )
    async with identity_session() as session:
        await LoginFlowRepository(session).insert(
            phase="authorize",
            flow_hash=_digest(state),
            binding_hash=binding_hash,
            ttl_s=AUTHORIZE_TTL_SECONDS,
            code_verifier=verifier,
            nonce_hash=_digest(nonce),
        )
    return google_oidc.authorize_url(
        settings.google_authorize_url,
        client_id=settings.google_oauth_client_id or "",
        redirect_uri=settings.google_redirect_uri,
        state=state,
        nonce=nonce,
        verifier=verifier,
    )


# ---- 2. callback ----


async def callback(
    *, code: str | None, state: str | None, error: str | None, ip: str | None
) -> str:
    """The dashboard location to 303 to: `?handle=` on success, `?error=cancelled` when the user
    backed out at Google, `?error=failed` for anything else (`unavailable` with the flag off)."""
    settings = get_settings()
    if not settings.google_oauth_enabled:
        return app_location(settings, error="unavailable")
    flow = None
    if state:
        async with identity_session() as session:
            flow = await LoginFlowRepository(session).consume(
                flow_hash=_digest(state), phase="authorize", binding_hash=None
            )
            if flow is None:
                await _failed(session, None, "state_invalid", ip)
    if error is not None:
        return app_location(settings, error="cancelled" if error == "access_denied" else "failed")
    if flow is None or not code or flow.code_verifier is None or flow.nonce_hash is None:
        return app_location(settings, error="failed")
    reason, identity = await _identity(settings, code, flow.code_verifier, flow.nonce_hash)
    async with identity_session() as session:
        if identity is None:
            await _failed(session, None, reason or "google_token_invalid", ip)
            return app_location(settings, error="failed")
        handle = google_oidc.new_token()
        await LoginFlowRepository(session).insert(
            phase="handle",
            flow_hash=_digest(handle),
            binding_hash=flow.binding_hash,
            ttl_s=HANDLE_TTL_SECONDS,
            identity=identity,
        )
    return app_location(settings, handle=handle)


async def _identity(
    settings: Settings, code: str, verifier: str, nonce_hash: bytes
) -> tuple[str | None, FlowIdentity | None]:
    """The verified, authoritative identity, or the audit reason it was refused."""
    try:
        id_token = await exchange_code(
            token_url=settings.google_token_url,
            code=code,
            verifier=verifier,
            client_id=settings.google_oauth_client_id or "",
            client_secret=settings.google_oauth_client_secret or "",
            redirect_uri=settings.google_redirect_uri,
        )
        verified = await _verify(settings, id_token.get_secret_value(), nonce_hash)
    except OAuthError as exc:
        log.warning("auth.google_exchange_failed", error_class=type(exc).__name__)
        return "google_exchange_failed", None
    except google_oidc.InvalidIdToken as exc:
        log.warning("auth.google_token_invalid", check=exc.reason)
        return "google_token_invalid", None
    except ValueError:  # an unreadable JWKS document
        log.warning("auth.google_token_invalid", check="jwks")
        return "google_token_invalid", None
    if not google_oidc.is_authoritative(verified.email, verified.hd):
        return "google_not_authoritative", None
    return None, FlowIdentity(
        sub=verified.sub,
        email=verified.email,
        hd=verified.hd,
        display_name=verified.display_name,
        given_name=verified.given_name,
        family_name=verified.family_name,
    )


# ---- 3. complete ----


async def peek_email(handle: str, binding: str, phase: Literal["handle", "link", "details"]) -> str:
    """The email a live flow row carries (the login limiter's key), read before the consume.
    Raises `GoogleDisabled` or `FlowInvalid`."""
    _enabled()
    async with identity_session() as session:
        row = await LoginFlowRepository(session).peek(
            flow_hash=_digest(handle), phase=phase, binding_hash=_binding(binding)
        )
    if row is None or row.identity is None:
        raise FlowInvalid
    return row.identity.email


async def complete(
    handle: str, binding: str, *, ip: str | None, user_agent: str | None
) -> Completed | Failure:
    """Raises `GoogleDisabled` or `FlowInvalid`; returns the outcome or the generic failure
    (committed before the caller answers)."""
    _enabled()
    async with identity_session() as session:
        flows = LoginFlowRepository(session)
        row = await flows.consume(
            flow_hash=_digest(handle), phase="handle", binding_hash=_binding(binding)
        )
        if row is None or row.identity is None:
            raise FlowInvalid
        identity = row.identity
        user, by_sub = await _find_user(session, identity)
        situation = await _situation(session, identity, user, by_sub)
        decision = decide(situation)
        if decision.outcome is None:
            await _failed(session, user.id if user else None, decision.reason or "failed", ip)
            return Failure(decision.reason or "failed")
        if decision.outcome == "needs_details":
            details_handle = google_oidc.new_token()
            await flows.insert(
                phase="details",
                flow_hash=_digest(details_handle),
                binding_hash=row.binding_hash,
                ttl_s=DETAILS_TTL_SECONDS,
                identity=identity,
            )
            return Completed("needs_details", details=_details(identity, details_handle))
        if user is None:  # pragma: no cover - decide() answers needs_details for no user
            raise FlowInvalid
        if decision.outcome == "pending":
            latest = await SignupRepository(session).latest_for(user.id)
            return Completed("pending", signup=latest)
        if decision.outcome == "link_required":
            link_handle = google_oidc.new_token()
            await flows.insert(
                phase="link",
                flow_hash=_digest(link_handle),
                binding_hash=row.binding_hash,
                ttl_s=LINK_TTL_SECONDS,
                identity=identity,
            )
            return Completed("link_required", link=Link(identity.email, link_handle))
        result = await auth.sign_in_with_google(session, user, ip=ip, user_agent=user_agent)
        if isinstance(result, auth.Refusal) or result is None:
            reason = result.kind if isinstance(result, auth.Refusal) else "no_active_membership"
            await _failed(session, user.id, reason, ip)
            return Failure(reason)
        return Completed("sign_in", login=result)


async def _find_user(session: Any, identity: FlowIdentity) -> tuple[UserRow | None, bool]:
    """The user linked to the Google account, else the user with the email (app.user_id set)."""
    users = UserRepository(session)
    user_id = await users.google_user(identity.sub)
    if user_id is not None:
        await set_gucs(session, user_id=user_id)
        return await users.self_row(user_id), True
    await set_gucs(session, login_email=identity.email)
    found = await users.by_email(identity.email)
    if found is not None:
        await set_gucs(session, user_id=found.id)
    return found, False


async def _situation(
    session: Any, identity: FlowIdentity, user: UserRow | None, by_sub: bool
) -> Situation:
    if user is None:
        return Situation(by_sub=False, exists=False)
    memberships = await MembershipRepository(session).list_active_for_user(user.id)
    latest = await SignupRepository(session).latest_for(user.id)
    return Situation(
        by_sub=by_sub,
        exists=True,
        is_platform_admin=user.is_platform_admin,
        is_active=user.is_active,
        has_password=user.hashed_password is not None,
        linked_to_other=user.google_sub is not None and user.google_sub != identity.sub,
        has_membership=bool(memberships),
        has_request=latest is not None,
    )


def _details(identity: FlowIdentity, details_handle: str) -> Details:
    return Details(
        email=identity.email,
        owner_name=identity.display_name,
        given_name=identity.given_name,
        family_name=identity.family_name,
        suggested_brand_name=suggest_brand(identity.email) if identity.hd else None,
        details_handle=details_handle,
    )


# ---- 4. link ----


async def link(
    link_handle: str, binding: str, password: str, *, ip: str | None, user_agent: str | None
) -> auth.SignInResult | None:
    """The password step of `link_required`: the ordinary password sign-in (counted, locked,
    audited as today) with the Google account riding on it; it is linked only when the sign-in
    ends with tokens. The link row stays usable until it expires or the sign-in passes. None is
    the 401. Raises `GoogleDisabled` or `FlowInvalid`."""
    _enabled()
    flow_hash, binding_hash = _digest(link_handle), _binding(binding)
    async with identity_session() as session:
        row = await LoginFlowRepository(session).peek(
            flow_hash=flow_hash, phase="link", binding_hash=binding_hash
        )
    if row is None or row.identity is None:
        raise FlowInvalid
    result = await auth.login(
        row.identity.email,
        password,
        ip=ip,
        user_agent=user_agent,
        link_sub=row.identity.sub,
    )
    if result is not None:
        async with identity_session() as session:
            await LoginFlowRepository(session).consume(
                flow_hash=flow_hash, phase="link", binding_hash=binding_hash
            )
    return result


# ---- 5. sign-up details ----


async def signup_details(
    details_handle: str,
    binding: str,
    *,
    brand_name: str,
    owner_name: str,
    phone: str | None,
    website: str | None,
    ip: str | None,
) -> SignupRow | Failure:
    """A new Google user and their pending sign-up request (ADR-0019 approval still applies).
    The email and the Google account are checked again (a password sign-up of the same email may
    have landed meanwhile): taken is the generic failure. Raises `GoogleDisabled` or
    `FlowInvalid`."""
    _enabled()
    async with identity_session() as session:
        row = await LoginFlowRepository(session).consume(
            flow_hash=_digest(details_handle), phase="details", binding_hash=_binding(binding)
        )
        if row is None or row.identity is None:
            raise FlowInvalid
        identity = row.identity
        await set_gucs(session, login_email=identity.email)
        requests = SignupRepository(session)
        request_id = await requests.create_google_applicant(
            email=identity.email,
            sub=identity.sub,
            display_name=owner_name,
            brand_name=brand_name,
            owner_name=owner_name,
            phone=phone,
            website=website,
        )
        user = await UserRepository(session).by_email(identity.email)
        if request_id is None or user is None:
            await _failed(session, None, "google_signup_taken", ip)
            return Failure("google_signup_taken")
        await set_gucs(session, user_id=user.id)
        await AuditRepository(session).write(
            tenant_id=None,
            actor_type="system",
            actor_user_id=None,
            action="signup.requested",
            entity_type="user",
            entity_id=user.id,
            reason="google",
            ip=auth.inet(ip),
        )
        created = await requests.latest_for(user.id)
    if created is None:  # pragma: no cover - the request was just written
        raise RuntimeError("a new Google sign-up request is not readable")
    return created


__all__ = [
    "CONSUMER_DOMAINS",
    "UNAVAILABLE_LOCATION",
    "Completed",
    "Decision",
    "Details",
    "Failure",
    "FlowInvalid",
    "GoogleDisabled",
    "Link",
    "Situation",
    "app_location",
    "callback",
    "complete",
    "decide",
    "link",
    "peek_email",
    "signup_details",
    "start",
    "suggest_brand",
]
