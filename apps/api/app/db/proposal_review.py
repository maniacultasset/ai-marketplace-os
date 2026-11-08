"""The staff review of AI-drafted error-map proposals (SI13 T14; P:B3, D6; CP-D4, CP-D5, CP-D17,
CP-D20). Run by `app.cli errors proposals approve|reject` on the migrator URL, one transaction.

`approve`:
1. `--by` must be an active platform admin (resolved by email); anyone else is refused.
2. The proposal must be `proposed` (an `approved` one is a replay: no second row).
3. The final row is the stored draft with the staff overrides merged in, and it passes the full
   `diagnosis.check` again (field index, critical keys, `ErrorCode`/`ErrorClass` membership,
   every text rule): `app_rw` can write a `proposed` row's text columns, so nothing stored is
   trusted. A map row also needs an error code and class.
4. The map row: the next version of the code, `retry_class` from `ERROR_CODE_META`, `severity
   error`, `auto_fixable false`, no fix action, `policy_ref {source: "staff-approved-ai-proposal",
   proposal_id, approved_by: <user id>, observed: false}` (no email: the seed goes to git), written
   through `load_error_map_rows` (its own audit row); then the proposal `approved` and one audit
   row `marketplace_error_proposal.approved` (NULL tenant, `{status}` only).
`reject` stores the reviewer and a note (at most 500 characters) and audits
`marketplace_error_proposal.rejected`. Nothing here prints a tenant id.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.platform_loads import ErrorMapIn, _assume_migrator, load_error_map_rows
from app.services.resolution import diagnosis

POLICY_SOURCE: Final = "staff-approved-ai-proposal"
NOTE_MAX: Final = 500
BASE_FIELDS: Final = (
    "listing",
    "facts.category",
    "facts.identifier",
    "facts.brand_name",
    "facts.gender",
    "facts.mrp",
    "facts.selling_price",
    "content.title",
    "content.description",
    "content.bullets",
    "keywords.search_terms",
)


class ReviewRefused(Exception):  # noqa: N818 - the CLI prints the reason
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Approved:
    seed: dict[str, Any] | None
    replay: bool = False


async def _admin(connection: AsyncConnection, email: str) -> uuid.UUID:
    row = (
        await connection.execute(
            text(
                "SELECT id, is_platform_admin, is_active FROM users "
                "WHERE lower(email) = lower(:email)"
            ),
            {"email": email.strip()},
        )
    ).first()
    if row is None or not row.is_active or not row.is_platform_admin:
        raise ReviewRefused("--by must be an active platform admin")
    return uuid.UUID(str(row.id))


async def _locked(connection: AsyncConnection, proposal_id: uuid.UUID) -> Any:
    row = (
        await connection.execute(
            text("SELECT * FROM marketplace_error_proposals WHERE id = :id FOR UPDATE"),
            {"id": proposal_id},
        )
    ).first()
    if row is None:
        raise ReviewRefused("no such proposal")
    return row


async def field_index(connection: AsyncConnection) -> tuple[frozenset[str], frozenset[str]]:
    """The paths a proposal may name (the base paths plus every dictionary attribute) and the
    critical keys, as the diagnosis job builds them."""
    rows = (
        await connection.execute(text("SELECT key, is_critical FROM attribute_definitions"))
    ).all()
    paths = set(BASE_FIELDS) | {f"attributes.{r.key}" for r in rows}
    return frozenset(paths), frozenset(str(r.key) for r in rows if r.is_critical)


def _merged(row: Any, overrides: Mapping[str, str | None]) -> dict[str, Any]:
    merged = {
        "plain_cause": row.plain_cause,
        "suggested_fix": row.suggested_fix,
        "lane_class": row.lane_class,
        "error_code": row.error_code,
        "error_class": row.error_class,
        "field_hint": row.field_hint,
    }
    for key, value in overrides.items():
        if value is not None:
            merged[key] = value
    return merged


async def approve(
    connection: AsyncConnection,
    proposal_id: uuid.UUID,
    *,
    by: str,
    overrides: Mapping[str, str | None],
) -> Approved:
    await connection.run_sync(_assume_migrator)
    admin = await _admin(connection, by)
    row = await _locked(connection, proposal_id)
    if row.status == "approved":
        return Approved(seed=None, replay=True)
    if row.status != "proposed":
        raise ReviewRefused(f"only a proposed row can be approved (this one is {row.status})")
    final = _merged(row, overrides)
    try:
        out = diagnosis.DiagnosisOut.model_validate(final)
    except ValueError:
        raise ReviewRefused("the final row is not a valid draft") from None
    index, critical = await field_index(connection)
    checked = diagnosis.check(out, field_index=index, critical_keys=critical, secrets=())
    if isinstance(checked, diagnosis.Invalid):
        raise ReviewRefused(f"the final row fails the check ({checked.reason})")
    if checked.error_code is None or checked.error_class is None:
        raise ReviewRefused("a map row needs --error-code and --error-class")
    code = ErrorCode(checked.error_code)
    version = (
        await connection.execute(
            text(
                "SELECT coalesce(max(version), 0) + 1 FROM marketplace_error_map "
                "WHERE marketplace = :m AND match_on = 'code' AND raw_value = :v"
            ),
            {"m": row.marketplace, "v": row.raw_value},
        )
    ).scalar_one()
    seed = {
        "marketplace": row.marketplace,
        "match_on": "code",
        "raw_value": row.raw_value,
        "version": int(version),
        "error_code": code.value,
        "error_class": checked.error_class,
        "retry_class": ERROR_CODE_META[code].retry_class.value,
        "severity": "error",
        "auto_fixable": False,
        "fix_action": None,
        "field_hint": checked.field_hint,
        "plain_text": checked.plain_cause,
        "seller_action": checked.suggested_fix,
        "policy_ref": {
            "source": POLICY_SOURCE,
            "proposal_id": str(proposal_id),
            "approved_by": str(admin),
            "observed": False,
        },
        "enabled": True,
    }
    await load_error_map_rows(connection, [ErrorMapIn.model_validate(seed)])
    await connection.execute(
        text(
            "UPDATE marketplace_error_proposals SET status = 'approved', reviewed_by = :by, "
            "reviewed_at = now(), error_map_version = :version WHERE id = :id"
        ),
        {"by": admin, "version": int(version), "id": proposal_id},
    )
    await _audit(connection, proposal_id, "approved")
    return Approved(seed=seed)


async def reject(
    connection: AsyncConnection, proposal_id: uuid.UUID, *, by: str, note: str
) -> None:
    await connection.run_sync(_assume_migrator)
    if not note.strip() or len(note) > NOTE_MAX:
        raise ReviewRefused(f"--note must be 1 to {NOTE_MAX} characters")
    admin = await _admin(connection, by)
    row = await _locked(connection, proposal_id)
    if row.status != "proposed":
        raise ReviewRefused(f"only a proposed row can be rejected (this one is {row.status})")
    await connection.execute(
        text(
            "UPDATE marketplace_error_proposals SET status = 'rejected', reviewed_by = :by, "
            "reviewed_at = now(), review_note = :note WHERE id = :id"
        ),
        {"by": admin, "note": note.strip(), "id": proposal_id},
    )
    await _audit(connection, proposal_id, "rejected")


async def _audit(connection: AsyncConnection, proposal_id: uuid.UUID, status: str) -> None:
    await connection.execute(
        text(
            "SELECT audit(NULL, 'system', NULL, :action, 'marketplace_error_proposal', :id, NULL, "
            "CAST(:after AS jsonb), 'errors proposals', NULL, NULL, NULL, NULL)"
        ),
        {
            "action": f"marketplace_error_proposal.{status}",
            "id": proposal_id,
            "after": json.dumps({"status": status}),
        },
    )


LIST_COLUMNS: Final = (
    "id, marketplace, raw_value, status, tries, failure_reason, plain_cause, suggested_fix, "
    "lane_class, error_code, error_class, field_hint, prompt_version, created_at, updated_at, "
    "reviewed_at, error_map_version"
)


async def list_rows(session: Any, *, status: str | None, limit: int) -> list[dict[str, Any]]:
    """The proposals as plain dicts, newest first; no sample issue, agent run or job id (they
    point at tenant rows) and no tenant id."""
    where = "WHERE status = :status " if status else ""
    stmt = text(
        f"SELECT {LIST_COLUMNS} FROM public.marketplace_error_proposals {where}"  # noqa: S608
        "ORDER BY created_at DESC, id LIMIT :limit"
    )
    rows = (await session.execute(stmt, {"status": status, "limit": limit})).mappings().all()
    return [
        {k: (str(v) if v is not None and k != "tries" else v) for k, v in r.items()} for r in rows
    ]
