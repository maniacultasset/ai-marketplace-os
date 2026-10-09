"""S16 tester round 1 (property): every chunk the chunker emits and `check_chunks` accepts is a
valid `BrandChunkDTO` text, so `BrandChunkRepository.search` can never fail to build a DTO from a
row the brand code stored (a stored chunk the DTO refuses would break retrieval for the tenant)."""

from __future__ import annotations

import uuid

from app.services.brand_chunking import (
    MAX_DOCUMENT_CHUNKS,
    MAX_PROFILE_CHUNKS,
    ChunkingError,
    check_chunks,
    chunk_document,
    chunk_profile,
)
from hypothesis import given, settings
from hypothesis import strategies as st

# unicode with the troublesome classes over-weighted: separators, format, BOM, combining marks
_ODD = st.sampled_from(
    [chr(c) for c in (0x2028, 0x2029, 0x200B, 0xFEFF, 0xA0, 0x301, 0x964, 0x85, 0x0D, 0x09)]
)
_TEXT = st.lists(st.one_of(st.text(max_size=40), _ODD, st.just("\n\n"), st.just("- ")), max_size=60)


def _dto(text: str) -> None:
    from mkos_schemas.jobs import BrandChunkDTO

    BrandChunkDTO(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type="tone_rule",
        text=text,
        similarity=0.5,
        source_type="brand_document",
        source_id=uuid.uuid4(),
    )


@settings(max_examples=300, deadline=None)
@given(_TEXT)
def test_accepted_document_chunks_are_valid_dtos(parts: list[str]) -> None:
    drafts = chunk_document("".join(parts), "tone_rule", limit=MAX_DOCUMENT_CHUNKS)
    try:
        check_chunks(drafts, field="file", max_chunks=MAX_DOCUMENT_CHUNKS)
    except ChunkingError:
        return
    for draft in drafts:
        _dto(draft.text)


@settings(max_examples=200, deadline=None)
@given(
    st.one_of(st.none(), st.text(max_size=200)),
    st.lists(st.text(min_size=1, max_size=80), max_size=5),
    st.dictionaries(st.text(min_size=1, max_size=20), st.text(min_size=1, max_size=20), max_size=5),
)
def test_accepted_profile_chunks_are_valid_dtos(
    tone: str | None, guidelines: list[str], glossary: dict[str, str]
) -> None:
    drafts = chunk_profile(tone, guidelines, glossary)
    if not drafts:
        return
    try:
        check_chunks(drafts, field="profile", max_chunks=MAX_PROFILE_CHUNKS)
    except ChunkingError:
        return
    assert [d.index for d in drafts] == list(range(len(drafts)))  # the unique active slot
    for draft in drafts:
        _dto(draft.text)
