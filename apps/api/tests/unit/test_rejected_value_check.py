"""SI9 T11 (AC12; PLAN D12; CP-D14, CP-D26, CP-D27): the deterministic "facts changed, regenerate"
check. A listing that still uses a photo value the seller rejected gets one `FACT_MISMATCH` item
per field: ERROR on the attribute and on a non-generic title hit, WARNING elsewhere."""

from __future__ import annotations

from typing import Any

from app.services.rejected_values import (
    phrase_in,
    phrase_spans,
    rejected_value_items,
    with_rejections,
)
from hypothesis import given
from hypothesis import strategies as st
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.listing import CanonicalListing
from tests.adapters.contract import sample_listing


def _listing(
    title: str = "Cotton Kurta",
    *,
    bullets: list[str] | None = None,
    description: str = "A cotton kurta.",
    terms: list[str] | None = None,
    attributes: dict[str, Any] | None = None,
) -> CanonicalListing:
    base = sample_listing()
    return base.model_copy(
        update={
            "content": base.content.model_copy(
                update={
                    "title": title,
                    "bullets": bullets or ["Soft cotton", "Easy care", "Daily wear"],
                    "description": description,
                }
            ),
            "keywords": base.keywords.model_copy(update={"search_terms": terms or []}),
            "attributes": attributes or {"color": "black"},
        }
    )


def _hits(listing: CanonicalListing, rejections: dict[str, str]) -> dict[str, Severity]:
    return {i.field: i.severity for i in rejected_value_items(listing, rejections)}


def test_title_hit_is_one_error() -> None:
    (item,) = rejected_value_items(_listing("Classic V-Neck Kurta"), {"neckline": "v neck"})
    assert (item.field, item.error_code, item.severity, item.source) == (
        "content.title",
        ErrorCode.FACT_MISMATCH,
        Severity.ERROR,
        ErrorSource.RULES_ENGINE,
    )
    assert item.message == (
        "You rejected v neck as the neckline, but this listing still uses it. Regenerate the "
        "listing."
    )
    assert item.suggestion == "Regenerate the listing, or edit the text to remove it."
    assert item.value_submitted == "v neck"


def test_word_boundaries_and_folding() -> None:
    marker = {"neckline": "v neck"}
    assert _hits(_listing("A v-necked kurta"), marker) == {}
    for title in (
        "V NECK kurta",
        "v_neck kurta",
        "v\N{EN DASH}neck kurta",
        "\N{FULLWIDTH LATIN CAPITAL LETTER V}-neck kurta",
    ):
        assert _hits(_listing(title), marker) == {"content.title": Severity.ERROR}, title
    assert (
        _hits(
            _listing("\u0935\u0940 \u0928\u0947\u0915 \u0915\u0941\u0930\u094d\u0924\u093e"), marker
        )
        == {}
    )


def test_other_fields() -> None:
    marker = {"neckline": "v-neck"}
    listing = _listing(
        bullets=["Soft cotton", "Easy care", "A v-neck cut"],
        description="Cut with a v-neck.",
        terms=["kurta", "v neck kurta"],
        attributes={"neckline": "V-Neck"},
    )
    assert _hits(listing, marker) == {
        "content.bullets[2]": Severity.WARNING,
        "content.description": Severity.WARNING,
        "keywords.search_terms[1]": Severity.WARNING,
        "attributes.neckline": Severity.ERROR,
    }


def test_template_spelling_marker_hits_both_phrases() -> None:
    marker = {"fit": "Regular"}
    assert _hits(_listing(attributes={"fit": "regular fit"}), marker) == {
        "attributes.fit": Severity.ERROR
    }
    assert _hits(_listing("Regular Fit Kurta"), marker) == {"content.title": Severity.ERROR}


def test_two_markers_in_one_title_are_one_item() -> None:
    (item,) = rejected_value_items(
        _listing("Maroon V-Neck Kurta"), {"neckline": "v-neck", "color": "maroon"}
    )
    assert item.severity is Severity.ERROR
    assert " and " in item.message and item.message.endswith(
        "but this listing still uses them. Regenerate the listing."
    )
    assert len(item.message) <= 1000


def test_generic_and_non_generic_in_one_title_is_one_error() -> None:
    (item,) = rejected_value_items(
        _listing("Party V-Neck Kurta"), {"neckline": "v-neck", "occasion": "party"}
    )
    assert item.severity is Severity.ERROR


def test_no_rejections_no_items() -> None:
    assert rejected_value_items(_listing("Classic V-Neck Kurta"), {}) == []


# ---- AC12 false-positive cases ----


def test_longer_values_do_not_count() -> None:
    work = {"occasion": "work"}
    assert _hits(_listing("Zari Work Kurta"), work) == {}
    assert _hits(_listing(bullets=["Fine thread work", "b", "c"]), work) == {}
    assert _hits(_listing("Off White Kurta"), {"color": "white"}) == {}
    assert _hits(_listing("Navy Blue Kurta"), {"color": "blue"}) == {}
    assert _hits(_listing("Blue Kurta"), {"color": "blue"}) == {"content.title": Severity.ERROR}


def test_generic_values_only_warn() -> None:
    assert _hits(_listing(bullets=["Quality checked", "b", "c"]), {"pattern": "checked"}) == {
        "content.bullets[0]": Severity.WARNING
    }
    assert _hits(_listing("Party Kurta"), {"occasion": "party"}) == {
        "content.title": Severity.WARNING
    }
    pieces = {"number_of_pieces": "2"}
    assert _hits(_listing("Kurta Set of 2"), pieces) == {"content.title": Severity.WARNING}
    assert _hits(_listing(attributes={"number_of_pieces": 2}), pieces) == {
        "attributes.number_of_pieces": Severity.ERROR
    }


# ---- with_rejections ----


def _report(*items: ErrorItem) -> ErrorReport:
    blocking = [i for i in items if i.severity is Severity.ERROR]
    return ErrorReport(status="failed" if blocking else "passed", errors=list(items))


def _rule_warning() -> ErrorItem:
    return ErrorItem(
        field="content.description",
        error_code=ErrorCode.FIELD_TOO_LONG,
        severity=Severity.WARNING,
        message="m",
        source=ErrorSource.RULES_ENGINE,
        retryable=False,
    )


def test_with_rejections_error_fails_the_report() -> None:
    got = with_rejections(
        _report(_rule_warning()), _listing("V-Neck Kurta"), {"neckline": "v-neck"}
    )
    assert got.status == "failed" and not got.passed
    assert got.error_code is ErrorCode.FACT_MISMATCH
    assert [i.error_code for i in got.errors] == [ErrorCode.FIELD_TOO_LONG, ErrorCode.FACT_MISMATCH]


def test_with_rejections_warning_keeps_it_passed() -> None:
    listing = _listing(bullets=["A v-neck cut", "b", "c"])
    got = with_rejections(_report(), listing, {"neckline": "v-neck"})
    assert got.passed and [i.severity for i in got.errors] == [Severity.WARNING]
    only = with_rejections(_report(), listing, {"neckline": "v-neck"}, errors_only=True)
    assert only.errors == [] and only.passed


def test_with_rejections_nothing_returns_the_report() -> None:
    report = _report(_rule_warning())
    assert with_rejections(report, _listing(), {}) is report


# ---- properties ----

_WORDS = st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=0, max_size=60)


@given(_WORDS)
def test_text_without_the_phrase_never_hits(text: str) -> None:
    if "v neck" in " ".join(text.split()):
        return
    assert not phrase_in(text, "v-neck")


@given(_WORDS, _WORDS)
def test_the_phrase_between_spaces_hits(left: str, right: str) -> None:
    assert phrase_spans(f"{left} V-Neck {right}", "v neck")
