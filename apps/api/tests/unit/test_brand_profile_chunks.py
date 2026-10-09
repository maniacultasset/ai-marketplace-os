"""S16 T4 (AC2, AC5; R10280): profile chunks, and the seam and credential checks every chunk
passes before it is embedded (invariant 6)."""

from __future__ import annotations

import uuid

import pytest
from app.services.brand_chunking import (
    ChunkDraft,
    ChunkingError,
    check_chunks,
    chunk_document,
    chunk_profile,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.jobs import BrandChunkDTO


def _refused(text: str) -> ChunkingError:
    drafts = chunk_document(f"Our voice is warm.\n\nNote: {text} keep it safe.", "tone_rule")
    with pytest.raises(ChunkingError) as info:
        check_chunks(drafts, field="file", max_chunks=500)
    return info.value


def test_profile_one_tone_rule_per_guideline() -> None:
    drafts = chunk_profile(None, ["Never claim best.", "Name the fabric first."], None)
    assert [(d.index, d.type, d.text) for d in drafts] == [
        (0, "tone_rule", "Never claim best."),
        (1, "tone_rule", "Name the fabric first."),
    ]


def test_profile_glossary_sentence() -> None:
    drafts = chunk_profile(None, None, {"kurta outfit": "kurta set"})
    assert [(d.type, d.text) for d in drafts] == [
        ("glossary", "Say 'kurta set' not 'kurta outfit'.")
    ]


def test_profile_glossary_sorted_stable() -> None:
    first = chunk_profile(None, ["g"], {"zari": "zari work", "anarkali": "anarkali suit"})
    second = chunk_profile(None, ["g"], {"anarkali": "anarkali suit", "zari": "zari work"})
    assert first == second
    assert [d.text for d in first] == [
        "g",
        "Say 'anarkali suit' not 'anarkali'.",
        "Say 'zari work' not 'zari'.",
    ]


def test_profile_tone_split() -> None:
    drafts = chunk_profile("Warm and plain.\n\n- Short sentences.", ["Rule one."], {"a": "b"})
    assert [(d.index, d.type, d.text) for d in drafts] == [
        (0, "tone_rule", "Rule one."),
        (1, "glossary", "Say 'b' not 'a'."),
        (2, "tone_rule", "Warm and plain."),
        (3, "tone_rule", "Short sentences."),
    ]


def test_profile_empty_profile_no_chunks() -> None:
    assert chunk_profile(None, None, None) == []
    assert chunk_profile("", [], {}) == []


def test_profile_repeats_dropped() -> None:
    drafts = chunk_profile("Rule one.", ["Rule one.", "Rule one."], None)
    assert [d.text for d in drafts] == ["Rule one."]


def test_check_chunks_zero_and_501() -> None:
    with pytest.raises(ChunkingError, match="no text"):
        check_chunks([], field="file", max_chunks=500)
    many = chunk_document("\n".join(f"- unit {i}" for i in range(501)), "tone_rule")
    with pytest.raises(ChunkingError) as info:
        check_chunks(many, field="file", max_chunks=500)
    assert info.value.reason == "more than 500 chunks"
    check_chunks(many[:500], field="file", max_chunks=500)


def test_check_chunks_secret_shaped() -> None:
    error = _refused('{"api_key": "x"}')
    assert error.field == "file"
    assert error.reason == "chunk 1 contains a forbidden key"
    assert "api_key" not in str(error) and '"x"' not in str(error)


def test_check_chunks_plain_secret_word_ok() -> None:
    drafts = chunk_document("Our secret sauce is the token of trust.", "tone_rule")
    check_chunks(drafts, field="file", max_chunks=500)


CREDENTIALS = {
    "atzr": "Atzr|" + "IwEBIA1b2c3d4e5f6g7h8i9j0",
    "atza": "Atza|" + "IwEBIA1b2c3d4e5f6g7h8i9j0",
    "amzn1_cs": "amzn1.oa2-cs.v1." + "0123456789abcdef0123",
    "sk": "sk-" + "proj-abcdefghijklmnopqrst1234",
    "akia": "AKIA" + "ABCDEFGHIJKLMNOP",
    "asia": "ASIA" + "ABCDEFGHIJKLMNOP",
    "ghp": "ghp_" + "a" * 36,
    "pem": "-----BEGIN " + "PRIVATE KEY-----",
    "jwt": "eyJ" + "hbGciOiJIUzI1.eyJzdWIiOiIxMjM0.abcdefghijk",
    "bearer": "Bearer " + "abcdefghijklmnop1234",
    "url_userinfo": "https://" + "user:hunter2@example.com/x",
}


@pytest.mark.parametrize("name", sorted(CREDENTIALS))
def test_check_chunks_credential_value(name: str) -> None:
    value = CREDENTIALS[name]
    error = _refused(value)
    assert error.reason == "chunk 1 contains a credential-shaped value"
    assert value not in str(error) and "keep it safe" not in str(error)


@pytest.mark.parametrize(
    "text",
    ["task-based", "sk-ill", "Bearer of good news", "eyJ alone", "https://example.com/a:b"],
)
def test_check_chunks_prose_lookalike_ok(text: str) -> None:
    check_chunks(chunk_document(f"We value {text} work.", "tone_rule"), field="f", max_chunks=5)


_PROSE = st.text(
    alphabet=st.characters(categories=("L", "N", "Zs", "P"), exclude_characters='{}[]"'),
    min_size=1,
    max_size=600,
)


@settings(max_examples=100, deadline=None)
@given(_PROSE)
def test_drafts_validate_as_brand_chunk_dto(text: str) -> None:
    drafts = chunk_document(text, "compliance")
    try:
        check_chunks(drafts, field="file", max_chunks=500)
    except ChunkingError:
        return
    for draft in drafts:
        assert isinstance(draft, ChunkDraft)
        BrandChunkDTO(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            type=draft.type,
            text=draft.text,
            similarity=0.5,
            source_type="brand_document",
            source_id=uuid.uuid4(),
            chunk_index=draft.index,
        )
