"""Image candidates (SI7, ADR-0030 proposed): the pieces the create API and the candidate job
share.

- The cache key of a candidate is (product, kind, `source_hash`, `generator_key`):
  `source_hash` is the source photo's `clean_content_hash`, or the sha256 of the size chart and
  fabric as the product read shows them; `generator_key` names what makes the image (the fix or
  render version, or the edit model and prompt version), so a new model or version is a new
  candidate and an old one is never reused for it.
- `size_chart_source(facts)` reads the chart through the product read's own parse
  (`size_chart_view`, `fibres_view`), so the API and the job hash the same value.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.services.products import fibres_view, size_chart_view

__all__ = [
    "FIX_GENERATOR",
    "IMAGE_EDIT_PROMPT_VERSION",
    "SIZE_CHART_GENERATOR",
    "SizeChartSource",
    "edit_generator_key",
    "size_chart_source",
]

FIX_GENERATOR: Final = "fix/v1"
SIZE_CHART_GENERATOR: Final = "size_chart/v1"
# The pipeline owns the prompt; the backend names its version in the cache key only.
IMAGE_EDIT_PROMPT_VERSION: Final = "image_edit/white_bg/v1"


def edit_generator_key(model: str) -> str:
    return f"white_background|{model}|{IMAGE_EDIT_PROMPT_VERSION}"[:200]


@dataclass(frozen=True, slots=True)
class SizeChartSource:
    chart: Mapping[str, Any]
    fibres: Sequence[Mapping[str, Any]] | None
    source_hash: str


def size_chart_source(facts: Mapping[str, Any] | None) -> SizeChartSource | None:
    """The product's size chart and fabric as the product read shows them, and their hash; None
    when the product has no chart."""
    chart = size_chart_view(facts or {})
    if chart is None:
        return None
    fibres = fibres_view(facts or {})
    body = json.dumps(
        {"size_chart": chart, "fibres": fibres}, sort_keys=True, separators=(",", ":")
    )
    return SizeChartSource(
        chart=chart, fibres=fibres, source_hash=hashlib.sha256(body.encode()).hexdigest()
    )
