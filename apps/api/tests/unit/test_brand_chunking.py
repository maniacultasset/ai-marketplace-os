"""S16 T3 (AC2, DW1; T90): document chunking per meaningful unit, linear time on hostile input."""

from __future__ import annotations

import functools
import time
import unicodedata
from collections.abc import Callable

import pytest
from app.services import brand_chunking as bc
from app.services.brand_chunking import (
    MAX_CHUNK_CHARS,
    MAX_CHUNK_WORDS,
    ChunkingError,
    chunk_document,
    chunk_hash,
    normalize_text,
    split_units,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.timing import budget


def _texts(text: str) -> list[str]:
    return [d.text for d in chunk_document(text, "tone_rule")]


def test_split_blank_lines() -> None:
    assert split_units("First para\nstill first.\n\nSecond para.\n\n\n\nThird.") == [
        "First para still first.",
        "Second para.",
        "Third.",
    ]


def test_split_bullets_each_a_chunk() -> None:
    text = "Rules:\n- Say kurta set.\n* Never claim best.\n+ Name the fabric.\n• Keep it plain."
    assert split_units(text) == [
        "Rules:",
        "Say kurta set.",
        "Never claim best.",
        "Name the fabric.",
        "Keep it plain.",
    ]


def test_split_numbered_items() -> None:
    assert split_units("1. One\n2) Two\n10. Ten\ncontinued") == ["One", "Two", "Ten continued"]


def test_split_headings_dropped() -> None:
    assert split_units("# Voice\n## Tone\nWarm and plain.\n###### Six\nMore.") == [
        "Warm and plain.",
        "More.",
    ]
    assert split_units("#hashtag stays") == ["#hashtag stays"]


def test_split_setext_heading_dropped() -> None:
    assert split_units("Voice guide\n===========\nWarm.\n\nGlossary\n--------\nSay x.") == [
        "Warm.",
        "Say x.",
    ]
    assert split_units("Before.\n\n---\n\nAfter.") == ["Before.", "After."]


def test_split_markers_stripped() -> None:
    assert _texts("- one\n- two") == ["one", "two"]
    assert _texts("*emphasis* is prose") == ["*emphasis* is prose"]


def test_split_crlf_and_bom() -> None:
    assert split_units("﻿One\r\n\r\nTwo\rthree") == ["One", "Two three"]


def test_split_control_chars_removed() -> None:
    assert split_units("Wa\x00rm\x07 and\x1b pla\x7fin\x9f.") == ["Warm and plain."]
    assert normalize_text("a\tb") == "a b"


def test_split_nfc_equal_hash() -> None:
    composed = "Café au lait."
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    assert chunk_hash(composed) == chunk_hash(decomposed)
    assert len(chunk_document(f"{composed}\n\n{decomposed}", "tone_rule")) == 1


def test_split_repeat_dropped() -> None:
    drafts = chunk_document("Same.\n\nOther.\n\nSame.\n\n  Same.  ", "glossary")
    assert [d.text for d in drafts] == ["Same.", "Other."]
    assert {d.type for d in drafts} == {"glossary"}


def test_split_blank_only_empty() -> None:
    assert chunk_document(" \n\n\t\n﻿\x00", "tone_rule") == []
    assert chunk_document("", "tone_rule") == []
    assert chunk_document("# Only a heading\n-", "tone_rule") == []


def test_split_long_unit_sentence_split() -> None:
    unit = " ".join(" ".join(f"w{i}x{j}" for j in range(14)) + "." for i in range(30))
    drafts = chunk_document(unit, "tone_rule")
    assert len(drafts) == 3
    for draft in drafts:
        assert len(draft.text.split()) <= MAX_CHUNK_WORDS
        assert draft.text.endswith(".")
    assert sum(len(d.text.split()) for d in drafts) == 420  # 30 sentences of 14 words


def test_split_danda_sentence_split() -> None:
    unit = " ".join(" ".join(f"शब्द{i}x{j}" for j in range(19)) + "।" for i in range(15))
    drafts = chunk_document(unit, "tone_rule")
    assert len(drafts) == 2
    assert all(d.text.endswith("।") for d in drafts)


def test_split_overlong_sentence_word_split() -> None:
    unit = " ".join(f"w{i}" for i in range(450))  # no sentence end
    drafts = chunk_document(unit, "tone_rule")
    assert [len(d.text.split()) for d in drafts] == [200, 200, 50]


def test_split_overlong_word_char_split() -> None:
    word = "".join(str(i % 7) for i in range(4500))
    drafts = chunk_document(word, "tone_rule")
    assert [len(d.text) for d in drafts] == [2000, 2000, 500]
    assert "".join(d.text for d in drafts) == word
    # identical cuts are repeats, kept once
    assert [len(d.text) for d in chunk_document("x" * 4500, "tone_rule")] == [2000, 500]


def test_split_long_chars_few_words() -> None:
    unit = " ".join(str(i) * 300 for i in range(10))  # 10 words, 3009 chars
    drafts = chunk_document(unit, "tone_rule")
    assert len(drafts) == 2
    assert all(len(d.text) <= MAX_CHUNK_CHARS for d in drafts)


def test_split_indexes_contiguous() -> None:
    drafts = chunk_document("a.\n\nb.\n\na.\n\nc.\n\n- d", "compliance")
    assert [d.index for d in drafts] == list(range(len(drafts))) == [0, 1, 2, 3]


def test_split_unicode_kept() -> None:
    text = "हमारी आवाज़ गर्मजोशी भरी है।"
    assert _texts(text) == [unicodedata.normalize("NFC", text)]


def test_split_hash_vector() -> None:
    assert chunk_hash("Say 'kurta set' not 'kurta outfit'.") == chunk_hash(
        "  Say 'kurta set'   not 'kurta outfit'.  "
    )
    assert chunk_hash("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=3000))
def test_chunks_bounded_property(text: str) -> None:
    for draft in chunk_document(text, "tone_rule"):
        assert draft.text.strip()
        assert len(draft.text.split()) <= MAX_CHUNK_WORDS
        assert len(draft.text) <= MAX_CHUNK_CHARS


@settings(max_examples=100, deadline=None)
@given(st.text(alphabet=st.sampled_from(list("ab .!?\n-*1#=")), max_size=2000))
def test_chunking_deterministic_property(text: str) -> None:
    first = chunk_document(text, "glossary")
    assert first == chunk_document(text, "glossary")
    assert len({d.content_hash for d in first}) == len(first)


def test_chunk_document_limit_stops_early(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    real = bc.chunk_hash

    def spy(text: str) -> str:
        nonlocal calls
        calls += 1
        return real(text)

    monkeypatch.setattr(bc, "chunk_hash", spy)
    text = "\n".join(f"- unit {i}" for i in range(2000))
    drafts = chunk_document(text, "tone_rule", limit=500)
    assert len(drafts) == 501
    assert calls <= 501


WORST_CASES: dict[str, Callable[[int], str]] = {
    "no_spaces": lambda n: "x" * n,
    "bullets": lambda n: "- " * (n // 2),
    "punctuation": lambda n: ".!?" * (n // 3),
    "setext": lambda n: ("=" * 80 + "\n" + "-" * 80 + "\n") * (n // 162),
    "numbered": lambda n: "1. " * (n // 3),
}


def _chunk_bounded(text: str) -> None:
    try:
        drafts = chunk_document(text, "tone_rule", limit=500)
    except ChunkingError:
        pass
    else:
        assert len(drafts) <= 501


def _best_of(
    run: Callable[[], None], runs: int = 3, clock: Callable[[], float] = time.perf_counter
) -> float:
    """Fastest of `runs` timings: a descheduled or GC-paused sample under xdist load never sets it."""
    samples = []
    for _ in range(runs):
        start = clock()
        run()
        samples.append(clock() - start)
    return min(samples)


def test_best_of_ignores_one_stalled_sample() -> None:
    ticks = iter([0.0, 0.9, 1.0, 1.05, 2.0, 2.06])  # samples 0.9 (stalled), 0.05, 0.06
    calls = []
    assert _best_of(lambda: calls.append(1), clock=lambda: next(ticks)) == pytest.approx(0.05)
    assert len(calls) == 3


@pytest.mark.parametrize("case", sorted(WORST_CASES))
def test_chunk_document_worst_case_10mib_bounded(case: str) -> None:
    make = WORST_CASES[case]
    timings = []
    for size in (1 << 20, 10 << 20):
        text = make(size)
        timings.append(_best_of(functools.partial(_chunk_bounded, text)))
    small, large = timings
    assert large <= budget(5.0), (case, large)  # machine-scaled (tests/timing.py)
    assert large <= 15 * max(small, 0.01), (case, small, large)


def test_chunk_document_mostly_repeats_refused() -> None:
    with pytest.raises(ChunkingError) as info:
        chunk_document("- same\n" * 5000, "tone_rule", limit=500, field="file")
    assert (info.value.field, info.value.reason) == ("file", "more than 500 chunks")
    assert "same" not in str(info.value)
    assert len(chunk_document("- same\n" * 5000, "tone_rule")) == 1  # no limit: no refusal
