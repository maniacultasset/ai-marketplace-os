"""Team invites by copied link (SI5, ADR-0029): create, list and revoke (`team:manage`), the
public lookup and accept steps, and `join` for a signed-in person whose email matches."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError, password_rules_error
from app.api.pagination import PageInfo, PageParams, encode_cursor
from app.api.rate_limit import enforce_invite_limit, request_ip
from app.api.v1.deps import get_current_principal
from app.api.v1.routers.google import DISABLED_MESSAGE, FLOW_MESSAGE
from app.api.v1.schemas.invites import (
    InviteAccept,
    InviteAccepted,
    InviteCreate,
    InviteCreated,
    InviteJoined,
    InviteOut,
    InvitePage,
    InvitePreview,
    InviteTokenIn,
)
from app.api.v1.surface import OPS, route
from app.core.errors import ForbiddenError
from app.core.password_policy import PolicyError
from app.core.rbac import Principal
from app.services import google_signin, invites

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
StatusFilter = Literal["pending", "accepted", "revoked", "expired"]


def _out(row: invites.InviteRow) -> InviteOut:
    return InviteOut.model_validate(
        {
            "id": row.id,
            "email": row.email,
            "role": row.role,
            "status": row.status,
            "invited_by_user_id": row.invited_by_user_id,
            "expires_at": row.expires_at,
            "created_at": row.created_at,
            "accepted_at": row.accepted_at,
            "revoked_at": row.revoked_at,
        }
    )


def _forbidden() -> ApiError:
    return ApiError(403, ErrorCode.PERMISSION_DENIED)


@route(router, OPS["invites_create"])
async def invites_create(body: InviteCreate, principal: CurrentPrincipal) -> InviteCreated:
    """Invite a teammate by email and role. The answer holds `invite_url` once: copy it to the
    person (it works once, for 7 days). Only an owner invites an owner (403). The email of a
    member here, active or not, is 422 at `email`, and so is a 51st pending invite. A new invite
    for an email with an open one revokes the old link. Needs a recent step-up under
    `MFA_ENFORCEMENT=all`."""
    try:
        row, url = await invites.create_invite(principal, email=body.email, role=body.role)
    except ForbiddenError:
        raise _forbidden() from None
    return InviteCreated(invite=_out(row), invite_url=url)


@route(router, OPS["invites_list"])
async def invites_list(
    principal: CurrentPrincipal,
    page: Annotated[PageParams, Depends()],
    status: Annotated[StatusFilter | None, Query(description="Only this status.")] = None,
) -> InvitePage:
    """The tenant's invites, newest first; never a token or its hash."""
    found = await invites.list_invites(principal, status=status, limit=page.limit, after=page.after)
    last = found.items[-1] if found.items else None
    next_cursor = encode_cursor(last.created_at, last.id) if found.has_more and last else None
    return InvitePage(
        data=[_out(r) for r in found.items],
        page=PageInfo(next_cursor=next_cursor, has_more=found.has_more, limit=page.limit),
    )


@route(router, OPS["invites_revoke"])
async def invites_revoke(invite_id: UUID, principal: CurrentPrincipal) -> InviteOut:
    """Revoke a pending or expired invite; revoking it again answers the same row. An accepted
    invite is 409; an owner invite is revoked only by an owner (403); another tenant's is 404."""
    try:
        row = await invites.revoke_invite(principal, invite_id)
    except ForbiddenError:
        raise _forbidden() from None
    return _out(row)


@route(router, OPS["invites_lookup"])
async def invites_lookup(body: InviteTokenIn, request: Request) -> InvitePreview:
    """What an invite link is for: the brand, the role, the invited email and whether it has an
    account that can sign in (`existing`: sign in, then `invites_join`; `new`: `invites_accept`,
    which for an account that cannot sign in yet, such as a pending sign-up, takes that
    account's own password). Every
    refusal (unknown, used, revoked or expired link, a brand that is not active, an inviter who
    lost the role) is the same 404."""
    await enforce_invite_limit(request)
    found = await invites.lookup(body.token)
    return InvitePreview(
        tenant_name=found.tenant_name,
        role=found.role,
        email=found.email,
        expires_at=found.expires_at,
        account=found.account,
    )


@route(router, OPS["invites_accept"])
async def invites_accept(body: InviteAccept, request: Request) -> InviteAccepted:
    """Create the invited account and join the brand: a password (12+ characters, an upper-case
    and a lower-case letter, a digit and a symbol, not the name or the email's first part: 422
    with each broken rule) or the matching Google identity. An email whose account can sign in
    is 409: sign in, then open the link. An account that cannot sign in yet (a pending sign-up,
    or no active brand) joins with its own password: a wrong one is 401 and counts toward the
    lockout. The person then signs in as usual."""
    await enforce_invite_limit(request)
    if body.google is not None:
        try:
            done = await invites.accept_google(
                body.token,
                handle=body.google.handle.get_secret_value(),
                binding=body.google.binding.get_secret_value(),
                display_name=body.display_name,
            )
        except google_signin.GoogleDisabled:
            raise ApiError(501, ErrorCode.NOT_IMPLEMENTED, DISABLED_MESSAGE) from None
        except google_signin.FlowInvalid:
            raise ApiError(401, ErrorCode.OAUTH_STATE_INVALID, FLOW_MESSAGE) from None
        return InviteAccepted(email=done.email, tenant_name=done.tenant_name)
    if body.password is None:  # pragma: no cover - the model needs exactly one credential
        raise ApiError(422, ErrorCode.VALIDATION_FAILED)
    try:
        done = await invites.accept_password(
            body.token,
            password=body.password.get_secret_value(),
            display_name=body.display_name,
            ip=request_ip(request),
        )
    except PolicyError as exc:
        raise password_rules_error([b.message for b in exc.breaches]) from None
    except invites.AccountRefusedError:
        raise ApiError(401, ErrorCode.UNAUTHENTICATED) from None
    return InviteAccepted(email=done.email, tenant_name=done.tenant_name)


@route(router, OPS["invites_join"])
async def invites_join(
    body: InviteTokenIn, request: Request, principal: CurrentPrincipal
) -> InviteJoined:
    """Join the inviting brand with the signed-in account, when the invite names its email
    (anything else is the generic 404). Already a member there, active or not: 409. Then call
    `auth_switch_tenant` with the answered `tenant_id`."""
    await enforce_invite_limit(request)
    done = await invites.join(principal, body.token)
    return InviteJoined(tenant_id=done.tenant_id, role=done.role)
