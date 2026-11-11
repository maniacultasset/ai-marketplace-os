"""Maker-checker in the API (S19 T4; invariant 3; DATABASE.md:785, :829; T15). Pure: no I/O.

`contributor_chain(current, versions)` walks `parent_version_id` back from the current version,
collecting every `generated_by_user_id` and `edited_by_user_id`, and stops after the first
`generation` or full `regeneration` (`changed_paths` NULL), exactly as `lv_guard` walks it
(DATABASE.md:785 "since the last generation or full regeneration"): a partial regeneration, an
edit and an `import` that names a parent carry the contributors before them forward; a full
regeneration starts a new chain whose maker is the user who asked for it. A version with no
parent ends the walk. A cycle or a missing parent raises `ChainError` (a broken chain is never read
as "nobody contributed").

`approver_refusal(approver, chain)` is `MAKER_CHECKER_VIOLATION` when the approver is in the
chain. The database's `lv_guard` (MKC01) checks the same chain again as the backstop.

`approve_block(caller, role=, active=, chain=, owner_self_approval=)` (SI10 T16) is the one
predicate behind both the approve path and the review read's `can_approve`: `not_approver` (not an
active reviewer or owner; checked first, CP-D7), `own_work` (in the chain), else None.

ADR-0031 (owner decision 2026-11-11): when the brand's `tenants.owner_self_approval` is on, an
active `owner` in the chain is not blocked. `lv_guard` marks the approval
(`listing_versions.self_approved`) and the approve path records that mark; `is_self_approval` is
the review read's preview of it. A reviewer in the chain is always `own_work`, whatever the setting; operators
and admins never approve. `lv_guard` (MKC01) permits exactly the same case.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from mkos_schemas.codes import ErrorCode

CHAIN_ROOTS: Final = frozenset({"generation"})
APPROVER_ROLES: Final = frozenset({"reviewer", "owner"})
# ADR-0031: the only role that may approve its own work, and only under the brand's setting
SELF_APPROVER_ROLES: Final = frozenset({"owner"})
ApproveHint = Literal["own_work", "no_other_approver", "not_approver", "owner_can_enable"]
ApproveBlock = Literal["own_work", "not_approver"]


class ChainError(ValueError):
    """The version chain has a cycle or a missing parent."""


@dataclass(frozen=True, slots=True)
class VersionLink:
    id: uuid.UUID
    origin: str
    parent_version_id: uuid.UUID | None
    changed_paths: Sequence[str] | None
    generated_by_user_id: uuid.UUID
    edited_by_user_id: uuid.UUID | None


def _starts_chain(link: VersionLink) -> bool:
    if link.origin in CHAIN_ROOTS:
        return True
    return link.origin == "regeneration" and link.changed_paths is None


def contributor_chain(
    current: uuid.UUID, versions: Mapping[uuid.UUID, VersionLink]
) -> frozenset[uuid.UUID]:
    out: set[uuid.UUID] = set()
    seen: set[uuid.UUID] = set()
    walk: uuid.UUID | None = current
    while walk is not None:
        if walk in seen:
            raise ChainError("the version chain has a cycle")
        seen.add(walk)
        link = versions.get(walk)
        if link is None:
            raise ChainError("the version chain names a missing version")
        out.add(link.generated_by_user_id)
        if link.edited_by_user_id is not None:
            out.add(link.edited_by_user_id)
        if _starts_chain(link):
            break
        walk = link.parent_version_id
    return frozenset(out)


def approver_refusal(approver: uuid.UUID, chain: frozenset[uuid.UUID]) -> ErrorCode | None:
    return ErrorCode.MAKER_CHECKER_VIOLATION if approver in chain else None


def may_self_approve(role: str | None, *, owner_self_approval: bool) -> bool:
    """ADR-0031: the setting is on and the approver's membership role is `owner`."""
    return owner_self_approval and role in SELF_APPROVER_ROLES


def approve_block(
    caller: uuid.UUID,
    *,
    role: str | None,
    active: bool,
    chain: frozenset[uuid.UUID],
    owner_self_approval: bool = False,
) -> ApproveBlock | None:
    if not active or role not in APPROVER_ROLES:
        return "not_approver"
    if approver_refusal(caller, chain) is not None and not may_self_approve(
        role, owner_self_approval=owner_self_approval
    ):
        return "own_work"
    return None


def is_self_approval(caller: uuid.UUID, chain: frozenset[uuid.UUID]) -> bool:
    """True when an allowed approval is of the caller's own work (recorded, ADR-0031)."""
    return caller in chain
