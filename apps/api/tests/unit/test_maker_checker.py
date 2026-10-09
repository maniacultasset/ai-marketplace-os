"""S19 T4 (DW1, DW4; DATABASE.md:785, :829): the contributor chain and the approver refusal."""

from __future__ import annotations

import uuid

import pytest
from app.services.maker_checker import (
    ChainError,
    VersionLink,
    approver_refusal,
    contributor_chain,
)
from mkos_schemas.codes import ErrorCode

A, B, C, D = (uuid.uuid4() for _ in range(4))


def _v(
    origin: str,
    maker: uuid.UUID,
    parent: VersionLink | None = None,
    editor: uuid.UUID | None = None,
    paths: tuple[str, ...] | None = None,
) -> VersionLink:
    return VersionLink(
        id=uuid.uuid4(),
        origin=origin,
        parent_version_id=None if parent is None else parent.id,
        changed_paths=paths,
        generated_by_user_id=maker,
        edited_by_user_id=editor,
    )


def _chain(*versions: VersionLink) -> frozenset[uuid.UUID]:
    return contributor_chain(versions[-1].id, {v.id: v for v in versions})


def test_chain_generation_only() -> None:
    assert _chain(_v("generation", A)) == {A}


def test_chain_edit_on_generation() -> None:
    gen = _v("generation", A)
    edit = _v("edit", A, gen, editor=B, paths=("content",))
    assert _chain(gen, edit) == {A, B}


def test_chain_earlier_editor_in_chain() -> None:
    gen = _v("generation", A)
    e1 = _v("edit", A, gen, editor=B, paths=("content",))
    e2 = _v("edit", A, e1, editor=C, paths=("keywords",))
    assert _chain(gen, e1, e2) == {A, B, C}


def test_chain_full_regeneration_resets() -> None:
    gen = _v("generation", A)
    edit = _v("edit", A, gen, editor=B, paths=("content",))
    regen = _v("regeneration", C, edit)
    assert _chain(gen, edit, regen) == {C}


def test_chain_partial_regeneration_carries_editors() -> None:
    gen = _v("generation", A)
    edit = _v("edit", A, gen, editor=B, paths=("content",))
    partial = _v("regeneration", C, edit, paths=("content.title",))
    assert _chain(gen, edit, partial) == {A, B, C}


def test_chain_import_stops() -> None:
    gen = _v("generation", A)
    imported = _v("import", D, gen)
    assert _chain(gen, imported) == {D}


def test_chain_cycle_raises() -> None:
    one = _v("edit", A, None, editor=B, paths=("content",))
    two = _v("edit", A, one, editor=C, paths=("content",))
    looped = VersionLink(
        id=one.id,
        origin="edit",
        parent_version_id=two.id,
        changed_paths=("content",),
        generated_by_user_id=A,
        edited_by_user_id=B,
    )
    with pytest.raises(ChainError):
        contributor_chain(two.id, {looped.id: looped, two.id: two})


def test_chain_missing_parent_raises() -> None:
    gen = _v("generation", A)
    edit = _v("edit", A, gen, editor=B, paths=("content",))
    with pytest.raises(ChainError):
        contributor_chain(edit.id, {edit.id: edit})
    with pytest.raises(ChainError):
        contributor_chain(uuid.uuid4(), {})


def test_refusal_generator() -> None:
    assert approver_refusal(A, frozenset({A})) is ErrorCode.MAKER_CHECKER_VIOLATION


def test_refusal_latest_editor() -> None:
    gen = _v("generation", A)
    edit = _v("edit", A, gen, editor=B, paths=("content",))
    assert approver_refusal(B, _chain(gen, edit)) is ErrorCode.MAKER_CHECKER_VIOLATION


def test_refusal_earlier_editor() -> None:
    gen = _v("generation", A)
    e1 = _v("edit", A, gen, editor=B, paths=("content",))
    e2 = _v("edit", A, e1, editor=C, paths=("content",))
    assert approver_refusal(B, _chain(gen, e1, e2)) is ErrorCode.MAKER_CHECKER_VIOLATION


def test_refusal_outsider_none() -> None:
    gen = _v("generation", A)
    edit = _v("edit", A, gen, editor=B, paths=("content",))
    assert approver_refusal(D, _chain(gen, edit)) is None


def test_refusal_regenerator_is_maker() -> None:
    gen = _v("generation", A)
    regen = _v("regeneration", C, gen)
    chain = _chain(gen, regen)
    assert approver_refusal(C, chain) is ErrorCode.MAKER_CHECKER_VIOLATION
    assert approver_refusal(A, chain) is None  # the earlier maker is outside a full regeneration
