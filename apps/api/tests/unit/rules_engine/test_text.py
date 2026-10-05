"""S14a T2/T3: normalisation, measuring, the tokenizer, slots and value access (PLAN D3)."""

from __future__ import annotations

import unicodedata

from app.services.rules_engine.text import measure, normalise

FULLWIDTH_BEST = "".join(chr(0xFEE0 + ord(c)) for c in "BEST")
NBSP = chr(0xA0)
IDEOGRAPHIC_SPACE = chr(0x3000)


def test_normalise_nfkc_fullwidth() -> None:
    assert normalise(FULLWIDTH_BEST, case_sensitive=False) == "best"


def test_normalise_case_sensitive_keeps_case() -> None:
    assert normalise("Best  Deal", case_sensitive=True) == "Best Deal"


def test_normalise_collapses_tabs_newlines_nbsp() -> None:
    assert normalise(
        " free\t\n" + NBSP + " shipping" + IDEOGRAPHIC_SPACE + "now ", case_sensitive=False
    ) == ("free shipping now")


def test_normalise_empty_and_blank() -> None:
    assert normalise("", case_sensitive=False) == ""
    assert normalise(" \t\n ", case_sensitive=False) == ""


def test_measure_chars_counts_code_points() -> None:
    assert measure("😀", "chars") == 1
    nfc = unicodedata.normalize("NFC", "é")
    nfd = unicodedata.normalize("NFD", "é")
    assert len(nfd) == 2
    assert measure(nfc, "chars") == measure(nfd, "chars") == 1


def test_measure_bytes_utf8() -> None:
    assert measure("कुर्ता", "bytes") == len("कुर्ता".encode())
    assert measure("abc", "bytes") == 3


def test_measure_words() -> None:
    assert measure("  cotton   kurta\tset ", "words") == 3


def test_measure_list_joined_by_one_space() -> None:
    assert measure(["ab", "cd"], "chars") == 5
    assert measure(["ab", "cd"], "words") == 2


def test_measure_empty_list_is_zero() -> None:
    assert measure([], "chars") == 0
    assert measure([], "words") == 0
