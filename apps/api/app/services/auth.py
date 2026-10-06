"""Authentication (BACKEND-ARCHITECTURE.md §9.2; DATABASE.md:306-312; CODE-PLAN S10 Q10).

`probe_principal` turns verified access claims into the `Principal`: one membership probe inside
the token's tenant, and the role from the row (a demoted user's old token carries the old role).
"""

from __future__ import annotations

from mkos_schemas.auth import JWTClaims

from app.core.rbac import Principal
from app.repositories.identity import MembershipRepository
from app.repositories.transactions import unit_of_work


async def probe_principal(claims: JWTClaims) -> Principal | None:
    """The caller, or None when the membership is inactive, the user inactive or the tenant
    neither `trial` nor `active`."""
    async with unit_of_work(claims.tid, user_id=claims.sub) as session:
        row = await MembershipRepository(session).probe(claims.tid, claims.sub)
    if row is None:
        return None
    return Principal(
        user_id=row.user_id,
        tenant_id=row.tenant_id,
        role=row.role,  # type: ignore[arg-type]
        email=row.email,
        jti=claims.jti,
    )
