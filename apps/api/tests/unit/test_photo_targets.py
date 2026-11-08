"""SI3 T3: the photo vocabulary and `build_targets` (pure)."""

from __future__ import annotations

import json
import subprocess
import sys
from base64 import b64encode
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.core.resolved_facts import ProductFacts, ResolvedFact
from app.services import photo_facts as pf
from app.services.rules_engine.text import find_terms
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.jobs import CategoryTemplate, ContextImage

ROOT = Path(__file__).resolve().parents[4]
SEED = ROOT / "apps/api/app/db/seed_data/platform_templates.json"
DICTIONARY = ROOT / "packages/schemas/src/mkos_schemas/data/attribute_dictionary.json"
RULES = ROOT / "packages/schemas/fixtures/rules.seed.json"
_JPEG = b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9").decode("ascii")
ENTRIES: dict[str, dict[str, Any]] = {
    e["key"]: e for e in json.loads(DICTIONARY.read_text(encoding="utf-8"))["attributes"]
}


def _templates() -> dict[tuple[str, str], CategoryTemplate]:
    rows = json.loads(SEED.read_text(encoding="utf-8"))
    return {
        (r["template"]["marketplace"], r["product_type"]): CategoryTemplate.model_validate(
            r["template"]
        )
        for r in rows
    }


TEMPLATES = _templates()
KURTA = TEMPLATES[("amazon", "KURTA")]
APPAREL = [t for t in TEMPLATES.values() if pf.APPAREL_SIGNAL & set(t.optional_attributes)]
PAIRS = [
    (("amazon", "KURTA"), ("flipkart", "kurta")),
    (("amazon", "SHIRT"), ("flipkart", "t_shirt")),
]


def _image(slot: Any = "main", n: int | None = None) -> ContextImage:
    return ContextImage(
        image_id=UUID(int=n) if n is not None else uuid4(),
        slot=slot,
        width=8,
        height=8,
        data_b64=_JPEG,
    )


def _fact(key: str, value: object, source: str = "seller", **extra: Any) -> ResolvedFact:
    return ResolvedFact(key=key, value=value, source=source, **extra)


def _facts(level: Any = "standalone", **attrs: ResolvedFact) -> ProductFacts:
    return ProductFacts(
        product_id=UUID(int=1),
        sku="SKU-1",
        level=level,
        attributes=dict(attrs),
    )


def _targets(
    facts: ProductFacts | None = None,
    template: CategoryTemplate = KURTA,
    images: list[ContextImage] | None = None,
    last: pf.PhotoSet | None = None,
) -> dict[str, Any]:
    images = [_image("main", 1), _image("secondary", 2)] if images is None else images
    current = pf.photo_set(images, {i.image_id: f"h{i.image_id.int}" for i in images})
    out = pf.build_targets(facts or _facts(), template, images, current_set=current, last_set=last)
    return {t.key: t for t in out}


# ---- AC2: only V1 keys, never a forbidden or critical fill ----


def test_ac2_every_dictionary_key_seller_filled() -> None:
    facts = _facts(**{k: _fact(k, "x") for k in ENTRIES})
    for template in APPAREL:
        targets = _targets(facts, template)
        assert set(targets) <= set(pf.V1_KEYS)
        assert not set(targets) & pf.FORBIDDEN_KEYS
        assert all(t.mode == "check" for t in targets.values())


def _assert_ac2(targets: dict[str, Any]) -> None:
    assert set(targets) <= set(pf.V1_KEYS)
    assert not set(targets) & pf.FORBIDDEN_KEYS
    for key, target in targets.items():
        if target.mode == "fill":
            entry = ENTRIES[key]
            assert not entry["is_critical"] and "ai" in entry["allowed_sources"]
    if "number_of_pieces" in targets:
        assert targets["number_of_pieces"].mode == "check"


_SOURCES = st.sampled_from(["seller", "document", "pim", "marketplace", "derived", "ai"])


@settings(max_examples=150, deadline=None)
@given(
    data=st.dictionaries(
        st.sampled_from(sorted(ENTRIES)),
        st.tuples(_SOURCES, st.booleans(), st.sampled_from(["round", "red", "3", "nonsense"])),
        max_size=len(ENTRIES),
    ),
    level=st.sampled_from(["standalone", "style", "variant"]),
)
def test_ac2_random_facts(data: dict[str, tuple[str, bool, str]], level: str) -> None:
    attrs = {
        k: _fact(k, v, src, confirmed_at="2026-11-07T00:00:00Z" if conf else None)
        for k, (src, conf, v) in data.items()
    }
    for template in APPAREL:
        _assert_ac2(_targets(_facts(level, **attrs), template))


def test_dictionary_entries_match_the_plan() -> None:
    assert ENTRIES["number_of_pieces"]["is_critical"] is True
    for key in pf.V1_KEYS:
        if key not in pf.CHECK_ONLY:
            assert ENTRIES[key]["is_critical"] is False
            assert "ai" in ENTRIES[key]["allowed_sources"]
    assert all(k in ENTRIES for k in pf.FORBIDDEN_KEYS - {"identifier"})


def test_import_time_cross_check_refuses_a_critical_key() -> None:
    code = (
        "import app.core.resolved_facts, importlib, sys, types\n"
        "src = open(sys.argv[1]).read().replace('V1_KEYS: Final = (', "
        "'V1_KEYS: Final = (\\n    \"fabric_type\",', 1)\n"
        "mod = types.ModuleType('photo_probe')\n"
        "exec(compile(src, 'photo_probe', 'exec'), mod.__dict__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(Path(pf.__file__))],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "photo key 'fabric_type'" in result.stderr


# ---- AC3: modes ----


def test_ac3_modes() -> None:
    at = "2026-11-07T00:00:00Z"
    cases = {
        "seller": (_fact("neckline", "Round"), "check"),
        "ai": (_fact("neckline", "v-neck", "ai"), "fill"),
        "ai_confirmed": (_fact("neckline", "v-neck", "ai", confirmed_at=at), "check"),
        "document": (_fact("neckline", "v-neck", "document"), "check"),
        "pim": (_fact("neckline", "v-neck", "pim"), "check"),
    }
    for fact, mode in cases.values():
        assert _targets(_facts(neckline=fact))["neckline"].mode == mode
    assert _targets()["neckline"].mode == "fill"


def test_ac3_style_and_variant() -> None:
    style = _targets(_facts("style"))
    assert "color" not in style and "pattern" not in style and "neckline" in style
    assert _targets(_facts("variant")) == {}
    standalone = _targets(_facts("standalone"))
    assert {"color", "pattern"} <= set(standalone)


# ---- AC3: skips ----


def test_ac3_no_images_or_only_label_and_size_chart() -> None:
    assert _targets(images=[]) == {}
    assert _targets(images=[_image("packaging_label", 1), _image("size_chart", 2)]) == {}


def test_ac3_same_set_skips_and_changes_bring_targets_back() -> None:
    a, b, c = _image("main", 1), _image("secondary", 2), _image("lifestyle", 3)
    hashes = {a.image_id: "ha", b.image_id: "hb", c.image_id: "hc"}
    seen = pf.photo_set([a, b], hashes)

    def run(images: list[ContextImage], h: dict[UUID, str]) -> list[Any]:
        return pf.build_targets(
            _facts(), KURTA, images, current_set=pf.photo_set(images, h), last_set=seen
        )

    assert run([a, b], hashes) == []
    assert run([b, a], hashes) == []  # a reorder within the same set
    assert run([a, b, c], hashes) != []  # added
    assert run([a], hashes) != []  # deleted
    assert run([a, b], {**hashes, b.image_id: "hb2"}) != []  # re-derived
    moved = _image("size_chart", 2)
    assert run([a, moved], {a.image_id: "ha", moved.image_id: "hb"}) != []  # out of a photo slot
    label = _image("packaging_label", 9)
    assert run([a, b, label], {**hashes, label.image_id: "hl"}) == []  # a label is not a photo


def test_ac3_lamp_and_tshirt_without_pieces() -> None:
    assert _targets(template=TEMPLATES[("amazon", "LAMP")]) == {}
    assert _targets(template=TEMPLATES[("flipkart", "table_lamp")]) == {}
    no_pieces = KURTA.model_copy(
        update={
            "optional_attributes": [
                k
                for k in TEMPLATES[("flipkart", "t_shirt")].optional_attributes
                if k != "number_of_pieces"
            ],
            "specs": {k: v for k, v in KURTA.specs.items() if k != "number_of_pieces"},
        }
    )
    assert "number_of_pieces" not in _targets(template=no_pieces)
    assert _targets()["number_of_pieces"].mode == "check"


# ---- allowed_for (CP-D18) ----


def test_allowed_for_narrows_to_the_marketplace_enum() -> None:
    colours = pf.allowed_for("color", KURTA)
    spec = KURTA.specs["color"]
    # SI9 CP-D16: the enum and the value_map keys, read in vocabulary space
    assert set(colours) == {
        pf.canonical("color", v) for v in [*(spec.allowed_values or []), *spec.value_map]
    }
    assert len(colours) == 29  # the SI2 kurta v3 colour enum
    assert not {"sky blue", "turquoise", "magenta"} & set(colours)
    assert len(colours) < len(pf.VOCAB["color"])
    assert pf.allowed_for("fit", KURTA) == ("regular fit", "slim fit", "relaxed fit")
    assert "oversized" not in pf.allowed_for("fit", KURTA)
    assert pf.allowed_for("pattern", KURTA) == pf.VOCAB["pattern"]
    assert len(pf.allowed_for("neckline", KURTA)) in (4, 5)


def test_allowed_for_unmappable_spec_drops_the_target() -> None:
    spec = KURTA.specs["neckline"].model_copy(
        update={"allowed_values": ["Zigzag", "Wavy"], "value_map": {}}
    )
    template = KURTA.model_copy(update={"specs": {**KURTA.specs, "neckline": spec}})
    assert pf.allowed_for("neckline", template) == ()
    assert "neckline" not in _targets(template=template)
    unset = KURTA.model_copy(
        update={
            "specs": {
                **KURTA.specs,
                "neckline": spec.model_copy(update={"allowed_values": None, "value_map": {}}),
            }
        }
    )
    assert pf.allowed_for("neckline", unset) == pf.VOCAB["neckline"]


# ---- the vocabulary ----


def test_vocabulary_shapes() -> None:
    assert set(pf.VOCAB) == set(pf.V1_KEYS)
    for key, values in pf.VOCAB.items():
        assert 1 <= len(values) <= 40, key
        assert len({pf.fold(v) for v in values}) == len(values), key
        assert all(0 < len(v) <= 40 and v == v.lower() for v in values), key
        for text, value in pf.SYNONYMS.get(key, {}).items():
            assert value in values, (key, text)


def test_every_seeded_apparel_value_maps_into_vocab() -> None:
    # The D4 review aid: lamps get no targets (CP-D16), so only apparel templates count.
    for template in APPAREL:
        for key in pf.V1_KEYS:
            spec = template.specs.get(key)
            for value in (spec.allowed_values or []) if spec else []:
                assert pf.canonical(key, value) is not None, (template.category, key, value)
            for text in spec.value_map if spec else {}:
                assert pf.canonical(key, text) is not None, (template.category, key, text)


def test_every_target_carries_the_whole_vocabulary() -> None:
    """SI9 CP-D16 (AC13): the pipeline reads in vocabulary space; the backend screens."""
    for template in APPAREL:
        for target in _targets(template=template).values():
            assert target.allowed_values == list(pf.VOCAB[target.key])


def test_pinned_marketplace_differences() -> None:
    """Values that map on one marketplace's template but not its pair (for the D4 review)."""
    found: dict[str, set[tuple[str, str]]] = {}
    for left, right in PAIRS:
        a, b = TEMPLATES[left], TEMPLATES[right]
        for key in pf.V1_KEYS:
            only_a = set(pf.allowed_for(key, a)) - set(pf.allowed_for(key, b))
            only_b = set(pf.allowed_for(key, b)) - set(pf.allowed_for(key, a))
            found.setdefault(f"{left[1]}/{right[1]}", set()).update(
                {("amazon", v) for v in only_a} | {("flipkart", v) for v in only_b}
            )
    # SI9: the value_map keys count too, so Flipkart reaches crew neck and polo collar
    expected = {("flipkart", "collared neck")}
    assert found == {"KURTA/kurta": expected, "SHIRT/t_shirt": expected}


def test_no_vocab_value_is_a_claim_word() -> None:
    seeds = json.loads(RULES.read_text(encoding="utf-8"))
    claims = [
        r["rule_def"]
        for pack in seeds
        for r in pack["rules"]
        if r["rule_def"].get("type") == "claim_requires_attribute"
    ]
    assert claims
    for rule_def in claims:
        for values in pf.VOCAB.values():
            for value in values:
                hits = find_terms(
                    value, rule_def["terms"], match=rule_def["match"], case_sensitive=False
                )
                assert hits == (), (value, hits)


# ---- canonical and fold ----


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("neckline", "Round", "round neck"),
        ("sleeve_length", "3/4 Sleeve", "three-quarter sleeve"),
        ("sleeve_length", "3/4th sleeves", "three-quarter sleeve"),
        ("neckline", "V\N{EN DASH}Neck", "v-neck"),
        ("neckline", "v_neck", "v-neck"),
        ("neckline", "chinese collar", "mandarin collar"),
        ("color", "Navy", "navy blue"),
        ("color", "  OFF-WHITE ", "off white"),
        ("fit", "slim", "slim fit"),
        ("neckline", "nonsense", None),
        ("neckline", True, None),
        ("number_of_pieces", 3, "3"),
        ("number_of_pieces", "3", "3"),
        ("number_of_pieces", 13, None),
        ("number_of_pieces", True, None),
        ("neckline", " ", None),
        ("mrp", "100", None),
    ],
)
def test_canonical(key: str, value: object, expected: str | None) -> None:
    assert pf.canonical(key, value) == expected


def test_photo_set_sorted_and_photo_slots_only() -> None:
    a, b, label = _image("secondary", 2), _image("main", 1), _image("packaging_label", 3)
    got = pf.photo_set([a, b, label], {a.image_id: "x", b.image_id: "y", label.image_id: "z"})
    assert got == ((str(b.image_id), "y"), (str(a.image_id), "x"))


# ---- SI9 T12 (AC13, AC15; CP-D16, CP-D18, CP-D25) ----

FLIPKART_KURTA = TEMPLATES[("flipkart", "kurta")]


def test_si9_subsets_in_vocabulary_space() -> None:
    assert set(pf.allowed_for("neckline", KURTA)) == {
        "crew neck",
        "mandarin collar",
        "polo collar",
        "round neck",
        "v-neck",
    }
    for template in (KURTA, FLIPKART_KURTA):
        assert set(pf.allowed_for("fit", template)) == {"regular fit", "slim fit", "relaxed fit"}
    assert "full sleeve" in pf.allowed_for("sleeve_length", KURTA)
    flipkart_sleeves = pf.allowed_for("sleeve_length", FLIPKART_KURTA)
    assert {"short sleeve", "three-quarter sleeve"} <= set(flipkart_sleeves)


def test_si9_template_spelling() -> None:
    assert pf.template_spelling("fit", "regular fit", KURTA) == "Regular"
    assert pf.template_spelling("sleeve_length", "full sleeve", KURTA) == "Long Sleeve"
    assert pf.template_spelling("sleeve_length", "short sleeve", FLIPKART_KURTA) == "Half Sleeve"
    assert (
        pf.template_spelling("sleeve_length", "three-quarter sleeve", FLIPKART_KURTA)
        == "3/4 Sleeve"
    )
    assert pf.template_spelling("pattern", "floral", KURTA) == "floral"  # no enum spec
    spec = KURTA.specs["fit"].model_copy(update={"allowed_values": [], "value_map": {"Slim": "S"}})
    only_map = KURTA.model_copy(update={"specs": {**KURTA.specs, "fit": spec}})
    assert pf.template_spelling("fit", "slim fit", only_map) == "Slim"


def test_si9_empty_listed_subset_has_no_target() -> None:
    spec = KURTA.specs["neckline"].model_copy(
        update={"allowed_values": ["Zigzag"], "value_map": {}}
    )
    template = KURTA.model_copy(update={"specs": {**KURTA.specs, "neckline": spec}})
    assert "neckline" not in _targets(template=template)


def test_si9_unlisted_key_fill_only() -> None:
    assert "garment_length" not in KURTA.specs
    assert _targets()["garment_length"].mode == "fill"
    filled = _targets(_facts(garment_length=_fact("garment_length", "knee length")))
    assert "garment_length" not in filled


def test_si9_exhaustive_seeded_templates() -> None:
    """(a) every apparel template gives a `fit` target with a non-empty subset, and every listed
    V1 key with an enum spec has a non-empty subset; (b) each subset value, written in the
    template's spelling, passes the Rules template check (no VALUE_NOT_ALLOWED)."""
    from app.services.rules_engine.template import template_allowed

    for template in APPAREL:
        assert "fit" in _targets(template=template), template.category
        assert pf.allowed_for("fit", template), template.category
        listed = pf._template_keys(template)
        for key in pf.V1_KEYS:
            if key in listed and template_allowed(template, key) is not None:
                assert pf.allowed_for(key, template), (template.category, key)
            accepted = {s.strip().casefold() for s in (template_allowed(template, key) or [])}
            if not accepted:
                continue
            for value in pf.allowed_for(key, template):
                spelled = pf.template_spelling(key, value, template)
                assert spelled.strip().casefold() in accepted, (template.category, key, value)


# ---- SI9 T13 (AC16): why the photos are not read ----


def test_si9_photo_read_skip() -> None:
    images = [_image("main", 1), _image("secondary", 2)]
    current = pf.photo_set(images, {i.image_id: f"h{i.image_id.int}" for i in images})

    def skip(**over: Any) -> Any:
        args: dict[str, Any] = {
            "facts": _facts(),
            "template": KURTA,
            "images": images,
            "current_set": current,
            "last_set": None,
        }
        args.update(over)
        return pf.photo_read_skip(
            args["facts"],
            args["template"],
            args["images"],
            current_set=args["current_set"],
            last_set=args["last_set"],
        )

    assert skip(facts=_facts("variant")) == "not_read"
    assert skip(template=TEMPLATES[("amazon", "LAMP")]) == "not_read"
    assert skip(images=[_image("packaging_label", 3)], current_set=()) == "not_read"
    assert skip(last_set=current) == "unchanged"
    assert skip() is None
