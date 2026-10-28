import inspect

from mkos_schemas.pipeline import run_listing_pipeline as schema_fn

import mkos_pipeline
import mkos_pipeline.graph as graph


def test_run_listing_pipeline_exported_from_both_paths() -> None:
    assert mkos_pipeline.run_listing_pipeline is graph.run_listing_pipeline


def test_signature_matches_schemas_protocol() -> None:
    ours = inspect.signature(graph.run_listing_pipeline)
    theirs = inspect.signature(schema_fn)
    assert list(ours.parameters) == list(theirs.parameters) == ["ctx", "deps"]
    assert inspect.iscoroutinefunction(graph.run_listing_pipeline)
