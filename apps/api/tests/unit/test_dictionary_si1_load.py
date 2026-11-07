"""SI1 T4: the shipped dictionary still loads through the sync's strict `DictionaryEntry` model,
with `size_chart_url` and the `fabric_composition` alias."""

from __future__ import annotations

from app.db.platform_loads import load_dictionary


def test_size_chart_url_loads_through_dictionary_entry() -> None:
    entries = {e.key: e for e in load_dictionary().attributes}
    entry = entries["size_chart_url"]
    assert entry.owner_levels == ["style", "standalone"]
    assert entry.storage == "attributes" and entry.value_type == "text"
    assert entries["fiber_composition"].input_aliases == ["fabric_composition"]
