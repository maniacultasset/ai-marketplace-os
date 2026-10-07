"""S14b T1 (AC 1, AC 5; D1): the stub honours the frozen seam signature and depends on the
contracts package only."""

from __future__ import annotations

import inspect
from importlib import metadata


def test_stub_signature_matches_contract() -> None:
    from mkos_pipeline.graph import run_listing_pipeline as stub
    from mkos_schemas.pipeline import run_listing_pipeline as contract

    assert inspect.iscoroutinefunction(stub)
    want = inspect.signature(contract, eval_str=True)
    got = inspect.signature(stub, eval_str=True)
    assert got == want
    assert [p.annotation for p in got.parameters.values()] == [
        p.annotation for p in want.parameters.values()
    ]
    assert got.return_annotation is want.return_annotation


def test_stub_dist_requires_only_schemas() -> None:
    assert metadata.requires("mkos-pipeline-stub") == ["mkos-schemas"]


def test_import_path_is_mkos_pipeline() -> None:
    from mkos_pipeline import run_listing_pipeline
    from mkos_pipeline.graph import run_listing_pipeline as graph_entry

    assert run_listing_pipeline is graph_entry
