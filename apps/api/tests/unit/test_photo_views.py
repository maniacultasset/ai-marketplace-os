"""Feedback 7 and 8: the photo view checklist per category and marketplace, the view status, the
size chart flags and the approval findings (`app/services/photo_views.py`). Pure."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from app.services import photo_views as pv
from app.services.image_gate import evaluate_image_gate, image_issue_report
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Marketplace, Severity
from mkos_schemas.listing import CanonicalListing
from pydantic import ValidationError

AMAZON = Marketplace.AMAZON
FLIPKART = Marketplace.FLIPKART
KURTA = "ethnic_wear_kurta_sets"
TEES = "tshirts"
LAMPS = "home_decor_lamps"


def photo(slot: str, role: str | None = None, status: str = "passed") -> pv.PhotoInput:
    return pv.PhotoInput(image_id=uuid.uuid4(), slot=slot, role=role, compliance_status=status)


def full_set(**over: Any) -> pv.ViewInputs:
    base: dict[str, Any] = {
        "images": (
            photo("main"),
            photo("secondary", "back"),
            photo("secondary", "close_up"),
            photo("size_chart"),
        ),
        "chart_sizes": ("S", "M", "L"),
        "variant_sizes": ("S", "M", "L"),
        "sized": True,
    }
    base.update(over)
    return pv.ViewInputs(**base)


def by_raw(items: tuple[Any, ...]) -> dict[str, Any]:
    return {i.raw_code: i for i in items}


# ---- the shipped data file ----


def test_shipped_file_loads_and_every_checklist_requires_front() -> None:
    data = pv.parse(json.loads(pv.DATA_PATH.read_text(encoding="utf-8")))
    assert (None, None) in data
    for rules in data.values():
        assert rules[0].view == "front"
        assert rules[0].requirement == "required"
        assert all(r.why and len(r.why) <= 160 for r in rules)
        order = [pv.VIEW_ORDER.index(r.view) for r in rules]
        assert order == sorted(order)


@pytest.mark.parametrize("category", [KURTA, TEES])
@pytest.mark.parametrize("marketplace", list(Marketplace))
def test_apparel_checklist(category: str, marketplace: Marketplace) -> None:
    rules = {r.view: r.requirement for r in pv.checklist(marketplace, category)}
    assert rules["front"] == "required"
    assert rules["back"] == "recommended"
    assert rules["close_up"] == "recommended"
    assert rules["size_chart"] == "required"
    assert pv.size_chart_required(marketplace, category)


def test_lamps_checklist_has_no_size_chart_and_only_front_required() -> None:
    rules = {r.view: r.requirement for r in pv.checklist(AMAZON, LAMPS)}
    assert rules["front"] == "required"
    assert "size_chart" not in rules
    assert {v for v, req in rules.items() if req == "required"} == {"front"}
    assert not pv.size_chart_required(AMAZON, LAMPS)


@pytest.mark.parametrize("category", [None, "uncategorized", "some_new_category"])
def test_unknown_category_takes_the_default(category: str | None) -> None:
    rules = {r.view: r.requirement for r in pv.checklist(AMAZON, category)}
    assert rules["front"] == "required"
    assert {v for v, req in rules.items() if req != "optional"} == {"front"}


# ---- the lookup order and the merge ----


def _data(*entries: dict[str, Any]) -> Any:
    default = {
        "marketplace": None,
        "category": None,
        "views": [{"view": "front", "requirement": "required", "why": "w"}],
    }
    return pv.parse({"version": 1, "checklists": [default, *entries]})


def _entry(marketplace: str | None, category: str | None, **views: str) -> dict[str, Any]:
    return {
        "marketplace": marketplace,
        "category": category,
        "views": [
            {"view": "front", "requirement": "required", "why": "front"},
            *({"view": v, "requirement": r, "why": f"{v} {r}"} for v, r in views.items()),
        ],
    }


def test_lookup_most_specific_first() -> None:
    data = _data(
        _entry("amazon", "kurtas", back="required"),
        _entry(None, "kurtas", back="recommended"),
        _entry("flipkart", None, lifestyle="recommended"),
    )
    assert pv.checklist(AMAZON, "kurtas", data)[1].requirement == "required"
    assert pv.checklist(FLIPKART, "kurtas", data)[1].requirement == "recommended"
    assert pv.checklist(FLIPKART, "other", data)[1].view == "lifestyle"
    assert [r.view for r in pv.checklist(AMAZON, "other", data)] == ["front"]


def test_merged_takes_the_strictest_and_keeps_each_marketplace() -> None:
    data = _data(
        _entry("amazon", "kurtas", back="required"),
        _entry("flipkart", "kurtas", back="optional", lifestyle="recommended"),
    )
    rows = {m.view: m for m in pv.merged("kurtas", data)}
    assert rows["back"].requirement == "required"
    assert rows["back"].why == "back required"
    assert dict(rows["back"].by_marketplace) == {AMAZON: "required", FLIPKART: "optional"}
    assert dict(rows["lifestyle"].by_marketplace) == {FLIPKART: "recommended"}
    assert [m.view for m in pv.merged("kurtas", data)] == ["front", "back", "lifestyle"]


@pytest.mark.parametrize(
    "bad",
    [
        {"version": 1, "checklists": [_entry("amazon", "x")]},  # no default
        {"version": 1, "checklists": [_entry(None, None), _entry(None, None)]},  # twice
        {"version": 1, "checklists": [_entry(None, None, front="recommended")]},  # front twice
        {
            "version": 1,
            "checklists": [{"views": [{"view": "back", "requirement": "required", "why": "w"}]}],
        },  # no front
        {
            "version": 1,
            "checklists": [
                {"views": [{"view": "front", "requirement": "required", "why": "w"}]},
                {
                    "category": "x",
                    "views": [{"view": "side", "requirement": "required", "why": "w"}],
                },
            ],
        },  # a view the system cannot store
        {
            "version": 1,
            "checklists": [{"views": [{"view": "front", "requirement": "must", "why": "w"}]}],
        },
    ],
)
def test_bad_files_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        pv.parse(bad)


# ---- views and statuses ----


@pytest.mark.parametrize(
    ("slot", "role", "view"),
    [
        ("main", None, "front"),
        ("secondary", "back", "back"),
        ("secondary", "close_up", "close_up"),
        ("secondary", None, None),
        ("size_chart", None, "size_chart"),
        ("lifestyle", None, "lifestyle"),
        ("packaging_label", None, "packaging_label"),
    ],
)
def test_view_of(slot: str, role: str | None, view: str | None) -> None:
    assert pv.view_of(slot, role) == view


def test_view_status() -> None:
    inputs = pv.ViewInputs(
        images=(
            photo("main", status="failed"),
            photo("main", status="pending"),
            photo("secondary", "back", status="failed"),
        )
    )
    assert pv.view_status("front", inputs) == "present"  # one photo is not failed
    assert pv.view_status("back", inputs) == "invalid"
    assert pv.view_status("close_up", inputs) == "missing"
    assert pv.view_status("size_chart", inputs) == "missing"
    typed = pv.ViewInputs(chart_sizes=("M",))
    assert pv.view_status("size_chart", typed) == "present"  # a saved chart needs no photo


def test_product_states_and_variant_drops_the_size_chart() -> None:
    inputs = pv.ViewInputs(images=(photo("main"), photo("secondary", "back")))
    states = pv.product_states(KURTA, inputs)
    assert [s.rule.view for s in states][:4] == ["front", "back", "close_up", "size_chart"]
    assert {s.rule.view: s.status for s in states}["back"] == "present"
    assert states[0].image_ids == (inputs.images[0].image_id,)
    variant = pv.ViewInputs(images=inputs.images, is_variant=True)
    assert "size_chart" not in [s.rule.view for s in pv.product_states(KURTA, variant)]


# ---- size flags ----


def test_size_flags_ignore_case_and_spaces_and_keep_order() -> None:
    lacking, unused = pv.size_flags(
        ["S", "M", "Free  Size", "XL"], ["m", "free size", "XXL", "xxl", "3XL"]
    )
    assert lacking == ["XXL", "3XL"]
    assert unused == ["S", "XL"]
    assert pv.size_flags(["S"], []) == ([], ["S"])
    assert pv.size_flags([], []) == ([], [])


def test_saved_chart_sizes_reads_only_a_seller_chart() -> None:
    table = {"unit": "cm", "basis": "garment", "rows": [{"size": "S"}, {"size": "M"}, {"x": 1}]}
    seller = {"size_measurements": {"source": "seller", "normalized": table}}
    assert pv.saved_chart_sizes(seller) == ("S", "M")
    assert (
        pv.saved_chart_sizes({"size_measurements": {"source": "ai", "normalized": table}}) is None
    )
    assert (
        pv.saved_chart_sizes({"size_measurements": {"source": "seller", "normalized": {}}}) is None
    )
    assert pv.saved_chart_sizes({}) is None
    assert pv.saved_chart_sizes(None) is None


def test_size_chart_check() -> None:
    assert pv.size_chart_check(KURTA, pv.ViewInputs(is_variant=True)) is None
    none = pv.size_chart_check(KURTA, pv.ViewInputs())
    assert none == pv.SizeChartCheck(required=True, saved=False, missing_sizes=(), unused_sizes=())
    lamp = pv.size_chart_check(LAMPS, pv.ViewInputs())
    assert lamp is not None and not lamp.required
    flagged = pv.size_chart_check(
        TEES, pv.ViewInputs(chart_sizes=("S", "M"), variant_sizes=("M", "L"), sized=True)
    )
    assert flagged == pv.SizeChartCheck(
        required=True, saved=True, missing_sizes=("L",), unused_sizes=("S",)
    )


# ---- findings ----


def test_complete_apparel_set_has_no_findings() -> None:
    assert pv.findings(AMAZON, KURTA, full_set()) == ()


def test_missing_required_blocks_and_recommended_warns() -> None:
    items = by_raw(pv.findings(AMAZON, KURTA, pv.ViewInputs(images=(photo("secondary"),))))
    assert set(items) == {
        "photo_view:front",
        "photo_view:back",
        "photo_view:close_up",
        "photo_view:size_chart",
    }
    front = items["photo_view:front"]
    assert front.severity is Severity.ERROR
    assert front.field == "listing"
    assert front.error_code is ErrorCode.IMAGE_NON_COMPLIANT
    assert front.source is ErrorSource.RULES_ENGINE
    assert front.marketplace is AMAZON
    assert front.suggestion
    assert items["photo_view:back"].severity is Severity.WARNING
    assert items["photo_view:close_up"].severity is Severity.WARNING
    chart = items["photo_view:size_chart"]
    assert chart.severity is Severity.ERROR
    assert "type it in" in chart.message


def test_invalid_view_is_left_to_the_image_gate() -> None:
    inputs = full_set(images=(photo("main", status="failed"), photo("size_chart")))
    items = by_raw(pv.findings(AMAZON, KURTA, inputs))
    assert "photo_view:front" not in items


def test_size_chart_photo_without_a_saved_chart_still_blocks() -> None:
    inputs = full_set(chart_sizes=None)
    (item,) = pv.findings(FLIPKART, TEES, inputs)
    assert item.raw_code == "photo_view:size_chart"
    assert item.severity is Severity.ERROR
    assert "Save the size chart" in item.message


def test_typed_chart_without_a_photo_passes() -> None:
    inputs = full_set(images=full_set().images[:3])
    assert pv.findings(AMAZON, KURTA, inputs) == ()


def test_size_mismatch_findings() -> None:
    inputs = full_set(chart_sizes=("S", "M", "XL"), variant_sizes=("S", "M", "L"))
    items = by_raw(pv.findings(AMAZON, KURTA, inputs))
    missing = items["size_chart:size_missing"]
    assert missing.severity is Severity.ERROR
    assert missing.field == "facts.size_measurements"
    assert missing.error_code is ErrorCode.CROSS_FIELD_MISMATCH
    assert "L" in missing.message
    unused = items["size_chart:size_unused"]
    assert unused.severity is Severity.WARNING
    assert "XL" in unused.message


def test_long_size_lists_are_cut() -> None:
    sizes = tuple(f"S{i}" for i in range(20))
    inputs = full_set(chart_sizes=("M",), variant_sizes=sizes)
    item, _unused = (
        i
        for i in pv.findings(AMAZON, KURTA, inputs)
        if (i.raw_code or "").startswith("size_chart:")
    )
    assert item.message.endswith("and more.")


def test_variant_and_lamps_skip_the_size_chart() -> None:
    variant = pv.ViewInputs(images=full_set().images[:3], is_variant=True)
    assert pv.findings(AMAZON, KURTA, variant) == ()
    lamp = pv.ViewInputs(images=(photo("main"),))
    items = by_raw(pv.findings(AMAZON, LAMPS, lamp))
    assert set(items) == {"photo_view:close_up", "photo_view:lifestyle"}
    assert all(i.severity is Severity.WARNING for i in items.values())


def test_recommended_size_chart() -> None:
    data = _data(_entry(None, "kurtas", size_chart="recommended"))
    none = pv.findings(AMAZON, "kurtas", pv.ViewInputs(images=(photo("main"),)), data)
    assert [(i.raw_code, i.severity) for i in none] == [("photo_view:size_chart", Severity.WARNING)]
    with_photo = pv.ViewInputs(images=(photo("main"), photo("size_chart")))
    assert pv.findings(AMAZON, "kurtas", with_photo, data) == ()
    mismatch = pv.ViewInputs(
        images=(photo("main"),), chart_sizes=("S",), variant_sizes=("M",), sized=True
    )
    severities = {i.raw_code: i.severity for i in pv.findings(AMAZON, "kurtas", mismatch, data)}
    assert severities == {
        "size_chart:size_missing": Severity.WARNING,
        "size_chart:size_unused": Severity.WARNING,
    }


def test_findings_are_deterministic() -> None:
    inputs = pv.ViewInputs(
        images=(photo("secondary"),), chart_sizes=("S",), variant_sizes=("M",), sized=True
    )
    assert pv.findings(AMAZON, KURTA, inputs) == pv.findings(AMAZON, KURTA, inputs)


# ---- the image gate keeps the findings at their severity ----


def _listing() -> CanonicalListing:
    return CanonicalListing.model_validate(
        {
            "product_id": str(uuid.uuid4()),
            "marketplace": "amazon",
            "category": KURTA,
            "content": {"title": "A kurta", "bullets": ["one"], "description": "d"},
        }
    )


def test_gate_and_issue_report_carry_the_view_findings() -> None:
    listing = _listing()
    views = pv.findings(AMAZON, KURTA, pv.ViewInputs(images=(photo("main"),)))
    gate = evaluate_image_gate(listing, {}, [], views)
    assert {i.raw_code for i in gate.blocking} == {"photo_view:size_chart"}
    assert {i.raw_code for i in gate.warnings} == {"photo_view:back", "photo_view:close_up"}
    report = image_issue_report(listing, {}, [], views)
    assert report.status == "failed"
    assert len(report.errors) == 3
    assert image_issue_report(listing, {}, []).status == "passed"


async def test_listing_findings_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError("no read while the switch is off")

    monkeypatch.setattr(pv, "enabled", lambda: False)
    assert await pv.listing_findings(_Boom(), _listing()) == ()


async def test_listing_findings_reads_the_product(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = _listing()
    product = type(
        "Row",
        (),
        {
            "id": listing.product_id,
            "parent_product_id": None,
            "variation_theme": "size",
            "attributes": {
                "size_measurements": {
                    "source": "seller",
                    "normalized": {"rows": [{"size": "S"}, {"size": "M"}]},
                }
            },
        },
    )()
    detail = type(
        "Detail",
        (),
        {"image_id": uuid.uuid4(), "slot": "main", "role": None, "compliance_status": "passed"},
    )()

    class _Products:
        async def get_by_id(self, pid: uuid.UUID) -> Any:
            return product if pid == listing.product_id else None

        async def child_sizes(self, pid: uuid.UUID) -> list[str]:
            return ["M", "L"]

    class _Images:
        async def details_for_product(self, pid: uuid.UUID) -> list[Any]:
            return [detail]

    uow = type("Uow", (), {"products": _Products(), "images": _Images()})()
    monkeypatch.setattr(pv, "enabled", lambda: True)
    items = by_raw(await pv.listing_findings(uow, listing))
    assert set(items) == {
        "photo_view:back",
        "photo_view:close_up",
        "size_chart:size_missing",
        "size_chart:size_unused",
    }
    gone = listing.model_copy(update={"product_id": uuid.uuid4()})
    assert {i.raw_code for i in await pv.listing_findings(uow, gone)} == {
        "photo_view:front",
        "photo_view:back",
        "photo_view:close_up",
        "photo_view:size_chart",
    }


def test_standalone_and_colour_styles_match_no_sizes() -> None:
    """Only a style split by size has sizes to match; a standalone or a colour-only style with a
    saved chart is complete."""
    unsized = full_set(variant_sizes=(), sized=False)
    assert pv.findings(AMAZON, KURTA, unsized) == ()
    check = pv.size_chart_check(KURTA, unsized)
    assert check == pv.SizeChartCheck(required=True, saved=True, missing_sizes=(), unused_sizes=())


@pytest.mark.parametrize(
    ("parent", "theme", "sized"),
    [(None, "size", True), (None, "size_color", True), (None, "color", False), (None, None, False)],
)
def test_inputs_of_marks_size_styles(parent: Any, theme: str | None, sized: bool) -> None:
    row = type(
        "Row", (), {"parent_product_id": parent, "variation_theme": theme, "attributes": {}}
    )()
    assert pv.inputs_of(row, [], ["M"]).sized is sized
    child = type(
        "Row", (), {"parent_product_id": uuid.uuid4(), "variation_theme": theme, "attributes": {}}
    )()
    got = pv.inputs_of(child, [], [])
    assert got.is_variant and not got.sized and got.chart_sizes is None
