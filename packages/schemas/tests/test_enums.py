"""Vocabulary tests against Canon sources."""

from __future__ import annotations

import re
from enum import StrEnum

import mkos_schemas.enums as enums_mod
from mkos_schemas.enums import (
    PUBLISHABLE_SLOTS,
    ErrorClass,
    ErrorSource,
    FixAction,
    Gate,
    ImageSlot,
    ItemMarketplaceState,
    JobKind,
    JobState,
    ListingStatus,
    Marketplace,
    MarketplaceState,
    PipelineStage,
    RetryClass,
    Severity,
)

from . import canon_sources

DELETED_A18 = {
    "loading_context",
    "retrieving_brand",
    "generating",
    "merging",
    "validating_schema",
    "validating_rules",
    "validating_vision",
    "correcting",
    "scoring",
    "awaiting_approval",
    "timeout",
}


def _values(cls: type[StrEnum]) -> set[str]:
    return {member.value for member in cls.__members__.values()}


def _enum_block_values(block: str, class_name: str) -> list[str]:
    out: list[str] = []
    in_class = False
    for line in block.splitlines():
        if f"class {class_name}" in line:
            in_class = True
            continue
        if in_class:
            if line.startswith("class ") or (line.strip() and not line[0].isspace()):
                break
            m = re.match(r'\s+\w+\s*=\s*"([^"]+)"', line)
            if m:
                out.append(m.group(1))
    if not out:
        raise ValueError(f"no values parsed for {class_name}")
    return out


def test_enum_listing_status_matches_canon() -> None:
    expected = set(canon_sources.canon_row_values("Listing status — persisted vocabulary"))
    assert _values(ListingStatus) == expected


def test_enum_pipeline_stage_matches_canon() -> None:
    block = canon_sources.addendum_block("18")
    from_block = set(_enum_block_values(block, "PipelineStage"))
    canon = set(
        canon_sources.canon_row_values("Pipeline stage — transient vocabulary and where it lives")
    )
    assert _values(PipelineStage) == from_block == canon


def test_enum_job_state_matches_canon() -> None:
    block = canon_sources.addendum_block("18")
    from_block = set(_enum_block_values(block, "JobState"))
    canon = set(canon_sources.canon_row_values("JobState vocabulary (wire + DB, lowercase)"))
    assert _values(JobState) == from_block == canon


def test_enum_error_source_matches_canon() -> None:
    block = canon_sources.addendum_block("19")
    sql = block[block.find("(") : block.rfind(")") + 1]
    expected = set(re.findall(r"'([^']+)'", sql))
    assert _values(ErrorSource) == expected


def test_enum_image_slot_matches_canon() -> None:
    row = canon_sources.database_cell_values("main\\|secondary\\|lifestyle")
    assert _values(ImageSlot) == set(row)


def test_enum_marketplace_matches_draft() -> None:
    block = canon_sources.draft_block("enums.py — the shared vocabularies")
    body = block[block.find("class Marketplace(") :]
    body = body[: body.find("\nclass ", 1)]
    expected = set(re.findall(r'= "([^"]+)"', body))
    assert expected == {"amazon", "flipkart"}
    assert _values(Marketplace) == expected


def test_enum_severity_matches_rules_check() -> None:
    row = canon_sources.database_cell_values("error\\|warning")
    assert _values(Severity) == set(row)


def test_enum_error_class_matches_rev3() -> None:
    row = canon_sources.database_cell_values("missing_data\\|invalid_value")
    assert _values(ErrorClass) == set(row)


def test_enum_retry_class_matches_rev3() -> None:
    row = canon_sources.database_cell_values("auto_retry\\|ai_correct")
    assert _values(RetryClass) == set(row)


def test_enum_fix_action_matches_rev3() -> None:
    row = canon_sources.database_cell_values("remap_value\\|reformat")
    assert _values(FixAction) == set(row)


def test_enum_marketplace_state_matches_rev3() -> None:
    row = canon_sources.database_cell_values("partially_live")
    assert _values(MarketplaceState) == set(row)


def test_enum_item_marketplace_state_is_seven_values() -> None:
    assert _values(ItemMarketplaceState) == _values(MarketplaceState) - {"partially_live"}


def test_enum_job_kind_matches_rev3() -> None:
    row = canon_sources.database_cell_values("listing_generate\\|listing_publish")
    assert _values(JobKind) == set(row)


def test_locale_is_en_in_only() -> None:
    assert enums_mod.Locale.__args__ == ("en-IN",)  # type: ignore[attr-defined]


def test_deleted_names_absent() -> None:
    assert not (_values(JobState) & DELETED_A18)
    assert not (_values(PipelineStage) & DELETED_A18)
    assert "timeout" not in _values(JobState)
    assert "rules" not in _values(ErrorSource)
    assert "swatch" not in _values(ImageSlot)
    assert "awaiting_approval" not in _values(PipelineStage)
    assert "publish" not in _values(PipelineStage)


def test_no_tenth_listing_status() -> None:
    assert "rejected" not in _values(ListingStatus)
    assert "partially_published" not in _values(ListingStatus)


def test_vocabulary_values_are_lowercase() -> None:
    for cls in (
        Marketplace,
        Severity,
        ErrorSource,
        JobState,
        ListingStatus,
        PipelineStage,
        ImageSlot,
        ErrorClass,
        RetryClass,
        FixAction,
        Gate,
        MarketplaceState,
        ItemMarketplaceState,
        JobKind,
    ):
        for v in _values(cls):
            assert v == v.lower(), cls


def test_publishable_slots_exclude_packaging_label() -> None:
    assert ImageSlot.PACKAGING_LABEL not in PUBLISHABLE_SLOTS
    assert len(PUBLISHABLE_SLOTS) == len(ImageSlot) - 1


def test_gate_is_the_nine_quality_gates_in_spec_order() -> None:
    # Spec "Quality Gates Before Submission" (docs/source/system-arch-llms-full.txt L1388-1424);
    # a closed set, so it is complete in 1.2.0 (docs/RULES.md §11).
    assert [g.value for g in Gate] == [
        "identity",
        "classification",
        "attributes",
        "variants",
        "media",
        "content",
        "compliance",
        "commercial",
        "submission",
    ]
