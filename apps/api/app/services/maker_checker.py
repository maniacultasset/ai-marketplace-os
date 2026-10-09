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
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from mkos_schemas.codes import ErrorCode

CHAIN_ROOTS: Final = frozenset({"generation"})


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
