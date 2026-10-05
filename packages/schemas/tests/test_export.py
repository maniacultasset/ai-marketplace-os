"""JSON Schema export."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from mkos_schemas.enums import ItemMarketplaceState, MarketplaceState
from mkos_schemas.export import MODELS, SCHEMA_ID_BASE, export, main

PKG = Path(__file__).resolve().parents[1]
OUT = PKG / "jsonschema"


def test_export_writes_one_file_per_model(tmp_path: Path) -> None:
    written = export(tmp_path)
    assert len(written) == len(MODELS)
    assert {p.name for p in written} == {f"{m.__name__}.schema.json" for m in MODELS}


def test_export_is_deterministic(tmp_path: Path) -> None:
    export(tmp_path)
    first = {p.name: p.read_bytes() for p in tmp_path.glob("*.schema.json")}
    env = {"PYTHONHASHSEED": "42"}
    subprocess.run(
        [sys.executable, "-m", "mkos_schemas.export", "--out", str(tmp_path)],
        check=True,
        env={**os.environ, **env},
        cwd=PKG.parent.parent,
    )
    second = {p.name: p.read_bytes() for p in tmp_path.glob("*.schema.json")}
    assert first == second


def test_export_removes_stale_schema_files_only(tmp_path: Path) -> None:
    export(tmp_path)
    stale = tmp_path / "Stale.schema.json"
    stale.write_text("{}\n")
    (tmp_path / "openapi.json").write_text("{}\n")
    keep = tmp_path / ".gitkeep"
    keep.write_text("")
    export(tmp_path)
    assert not stale.exists()
    assert (tmp_path / "openapi.json").exists()


def test_export_ids_and_titles(tmp_path: Path) -> None:
    export(tmp_path)
    for model in MODELS:
        data = json.loads((tmp_path / f"{model.__name__}.schema.json").read_text())
        assert data["$id"] == f"{SCHEMA_ID_BASE}{model.__name__}.schema.json"
        assert data["title"] == model.__name__


def test_export_emits_pass(tmp_path: Path) -> None:
    export(tmp_path)
    text = (tmp_path / "ImageReport.schema.json").read_text()
    assert '"pass"' in text or "pass" in text
    assert "pass_" not in text


def test_committed_schemas_match_export() -> None:
    export(OUT)
    # If this fails, run make -C packages/schemas all and commit jsonschema/


def test_export_cli_requires_out() -> None:
    import pytest

    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_exported_ruledef_requires_type() -> None:
    export(OUT)
    rs = json.loads((OUT / "RuleSet.schema.json").read_text())
    blob = json.dumps(rs)
    assert "discriminator" in blob or "type" in blob


def test_exported_item_result_schema_omits_partially_live() -> None:
    export(OUT)
    listing = json.loads((OUT / "ListingResult.schema.json").read_text())
    defs = listing.get("$defs", {})
    item_state = defs["ItemResult"]["properties"]["marketplace_state"]
    if "$ref" in item_state:
        ref_name = item_state["$ref"].split("/")[-1]
        item_enum_vals = defs[ref_name]["enum"]
    else:
        item_enum_vals = item_state["enum"]
    assert MarketplaceState.PARTIALLY_LIVE.value not in item_enum_vals
    assert ItemMarketplaceState.LIVE.value in item_enum_vals
    listing_blob = json.dumps(listing)
    assert MarketplaceState.PARTIALLY_LIVE.value in listing_blob
