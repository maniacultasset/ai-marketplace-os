from mkos_schemas.rules import AttributeSpec
from tests.fakes import FakeDeps, make_context

from mkos_pipeline.nodes.attribute import filter_draft
from mkos_pipeline.nodes.fact_backed import canonical_attribute_value, keep_attribute
from mkos_pipeline.schemas_local import AttributeDraft, AttributePick
from mkos_pipeline.state import GraphState


def test_invented_material_is_dropped() -> None:
    product = {"attributes": {"color": "indigo", "sleeve_length": "three_quarter"}}
    spec = AttributeSpec(allowed_values=["cotton"])
    assert keep_attribute("material", "organic cotton", product, spec) is False


def test_case_fold_keeps_fact_canonical_form() -> None:
    product = {"attributes": {"color": "indigo"}}
    spec = AttributeSpec(allowed_values=["indigo", "navy"])
    assert keep_attribute("color", "Indigo", product, spec) is True
    assert canonical_attribute_value("color", "Indigo", product, spec) == "indigo"


def test_allowed_value_spelling_wins_when_spec_has_it() -> None:
    product = {"attributes": {"color": "indigo"}}
    spec = AttributeSpec(allowed_values=["Indigo"])
    assert canonical_attribute_value("color", "Indigo", product, spec) == "Indigo"


def test_filter_draft_drops_invented_keys() -> None:
    ctx = make_context()
    state = GraphState(ctx=ctx, deps=FakeDeps(ctx), deadline_at=ctx.marketplace_rules.loaded_at)
    draft = AttributeDraft(
        attributes=[
            AttributePick(key="color", value="Indigo", source_fact="attributes.color"),
            AttributePick(key="material", value="organic cotton", source_fact="attributes.material"),
        ]
    )
    filtered = filter_draft(draft, state)
    keys = {pick.key for pick in filtered.attributes}
    assert "material" not in keys
    assert "color" in keys
    assert filtered.attributes[0].value == "indigo"
