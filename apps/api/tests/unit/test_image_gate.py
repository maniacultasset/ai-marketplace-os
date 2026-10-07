"""S17b T14 (DW3, AC3; PLAN D3, D4; CP5; C8): the pure image gate S19's approve calls."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.services.image_gate import NOT_CHECKED, evaluate_image_gate
from hypothesis import given
from hypothesis import strategies as st
from mkos_schemas.codes import ErrorCode, ImageIssueCode
from mkos_schemas.enums import ErrorSource, ImageSlot, Marketplace, Severity
from mkos_schemas.errors import ErrorReport
from mkos_schemas.images import ImageCheck, ImageIssue, ImageReport
from mkos_schemas.listing import CanonicalListing
from pydantic import ValidationError

NOW = datetime(2026, 10, 7, tzinfo=UTC)
P = uuid.uuid4()


def listing(*image_ids: uuid.UUID, marketplace: str = "amazon") -> CanonicalListing:
    return CanonicalListing.model_validate(
        {
            "product_id": str(P),
            "marketplace": marketplace,
            "category": "kurtas",
            "content": {"title": "A kurta", "bullets": ["one"], "description": "d"},
            "images": [
                {"image_id": str(i), "slot": "main" if n == 0 else "secondary"}
                for n, i in enumerate(image_ids)
            ],
        }
    )


def report(
    image_id: uuid.UUID,
    *,
    passed: bool = True,
    issues: tuple[str, ...] = (),
    marketplace: str = "amazon",
    method: str = "deterministic",
    checks: tuple[tuple[str, bool], ...] | None = None,
) -> ImageReport:
    found = [
        ImageIssue(code=ImageIssueCode(c), check=_check(c), suggestion="fix it") for c in issues
    ]
    made = checks if checks is not None else tuple((_check(c), False) for c in issues)
    return ImageReport(
        image_id=image_id,
        marketplace=Marketplace(marketplace),
        slot=ImageSlot.MAIN,
        pass_=passed,
        checks=[ImageCheck(check=c, method=method, passed=ok) for c, ok in made],
        issues=found,
        processed_at=NOW,
    )


_CHECKS = {
    "RESOLUTION_LOW": "resolution",
    "BACKGROUND_OFF": "background",
    "CORRUPT": "corruption",
    "WRONG_ASPECT": "aspect_ratio",
}


def _check(code: str) -> Any:
    return _CHECKS[code]


def test_vision_is_warning() -> None:
    """DW3: a passing stored report plus a pipeline report whose vision background check failed
    (pass=false) → nothing blocks, one vision warning."""
    image = uuid.uuid4()
    vision = report(image, passed=False, issues=("BACKGROUND_OFF",), method="vision")
    result = evaluate_image_gate(listing(image), {image: report(image)}, [vision])
    assert result.blocking == ()
    assert [(w.source, w.severity, w.raw_code) for w in result.warnings] == [
        (ErrorSource.VISION_MODEL, Severity.WARNING, "BACKGROUND_OFF")
    ]


def test_image_gate_stored_fail_blocks() -> None:
    image = uuid.uuid4()
    stored = report(image, passed=False, issues=("RESOLUTION_LOW", "WRONG_ASPECT"))
    result = evaluate_image_gate(listing(image), {image: stored}, [])
    assert [b.raw_code for b in result.blocking] == ["RESOLUTION_LOW", "WRONG_ASPECT"]
    assert all(
        b.error_code is ErrorCode.IMAGE_NON_COMPLIANT and b.field == "images[0]"
        for b in result.blocking
    )
    assert all(
        b.source is ErrorSource.RULES_ENGINE and b.suggestion == "fix it" for b in result.blocking
    )


def test_image_gate_pending_blocks() -> None:
    image = uuid.uuid4()
    result = evaluate_image_gate(listing(image), {}, [])
    assert [(b.message, b.source, b.retryable) for b in result.blocking] == [
        (NOT_CHECKED, ErrorSource.SYSTEM, True)
    ]


def test_image_gate_passing_warning_issue_is_warning() -> None:
    image = uuid.uuid4()
    stored = report(image, passed=True, issues=("BACKGROUND_OFF",))
    result = evaluate_image_gate(listing(image), {image: stored}, [])
    assert result.blocking == () and [w.raw_code for w in result.warnings] == ["BACKGROUND_OFF"]


def test_image_gate_other_marketplace_report_ignored() -> None:
    image = uuid.uuid4()
    result = evaluate_image_gate(listing(image), {image: report(image, marketplace="flipkart")}, [])
    assert [b.message for b in result.blocking] == [NOT_CHECKED]


def test_image_gate_unknown_image_id_blocks() -> None:
    known, unknown = uuid.uuid4(), uuid.uuid4()
    result = evaluate_image_gate(listing(known, unknown), {known: report(known)}, [])
    assert [b.field for b in result.blocking] == ["images[1]"]


def test_image_gate_pipeline_deterministic_check_ignored() -> None:
    image = uuid.uuid4()
    pipeline = report(image, passed=False, issues=("RESOLUTION_LOW",))  # deterministic
    result = evaluate_image_gate(listing(image), {image: report(image)}, [pipeline])
    assert result.blocking == () and result.warnings == ()


def test_image_gate_items_field_paths_legal() -> None:
    ids = [uuid.uuid4() for _ in range(3)]
    result = evaluate_image_gate(listing(*ids), {}, [])
    assert [b.field for b in result.blocking] == ["images[0]", "images[1]", "images[2]"]


def test_packaging_label_never_in_listing() -> None:
    with pytest.raises(ValidationError):
        CanonicalListing.model_validate(
            {
                "product_id": str(P),
                "marketplace": "amazon",
                "category": "kurtas",
                "content": {"title": "A kurta", "bullets": ["one"], "description": "d"},
                "images": [{"image_id": str(uuid.uuid4()), "slot": "packaging_label"}],
            }
        )


def test_image_gate_warning_items_have_warning_severity() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    stored = {
        a: report(a, passed=True, issues=("BACKGROUND_OFF",)),
        b: report(b, passed=False, issues=("CORRUPT",)),
    }
    vision = report(a, passed=False, issues=("BACKGROUND_OFF",), method="vision")
    result = evaluate_image_gate(listing(a, b), stored, [vision])
    assert result.warnings and all(w.severity is Severity.WARNING for w in result.warnings)
    assert result.blocking and all(x.severity is Severity.ERROR for x in result.blocking)


_codes = st.sampled_from(sorted(_CHECKS))


@st.composite
def _inputs(draw: Any) -> tuple[CanonicalListing, dict[uuid.UUID, ImageReport], list[ImageReport]]:
    ids = draw(st.lists(st.uuids(), min_size=0, max_size=4, unique=True))
    stored: dict[uuid.UUID, ImageReport] = {}
    for image in ids:
        if draw(st.booleans()):
            stored[image] = report(
                image,
                passed=draw(st.booleans()),
                issues=tuple(draw(st.lists(_codes, max_size=2, unique=True))),
                marketplace=draw(st.sampled_from(["amazon", "flipkart"])),
            )
    pipeline = [
        report(image, passed=draw(st.booleans()), issues=tuple(draw(st.lists(_codes, max_size=2, unique=True))),
               method="vision")
        for image in ids
        if draw(st.booleans())
    ]  # fmt: skip
    return listing(*ids), stored, pipeline


@given(_inputs())
def test_gate_vision_never_blocks(inputs: Any) -> None:
    lst, stored, pipeline = inputs
    with_vision = evaluate_image_gate(lst, stored, pipeline)
    without = evaluate_image_gate(lst, stored, [])
    assert with_vision.blocking == without.blocking


@given(_inputs())
def test_gate_never_raises(inputs: Any) -> None:
    evaluate_image_gate(*inputs)


@given(_inputs())
def test_gate_merged_report_blocking_equals_gate_blocking(inputs: Any) -> None:
    result = evaluate_image_gate(*inputs)
    merged = ErrorReport(status="failed", errors=[*result.blocking, *result.warnings])
    assert merged.blocking == list(result.blocking)
