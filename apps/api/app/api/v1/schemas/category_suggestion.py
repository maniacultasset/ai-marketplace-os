"""SI14: the category-suggestion answer (`POST /products/{product_id}/category-suggestion`)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from mkos_schemas.base import WireModel

CategorySuggestionAnswer = Literal["queued", "suggested", "unavailable", "failed"]


class CategorySuggestionAccepted(WireModel):
    """202 `queued` with the job; 200 `suggested` when the stored suggestion already matches the
    product's current text, photos and categories (no job); 200 `unavailable` while the switch is
    off or the pipeline has no classifier. Poll the product for `suggestions.category`."""

    product_id: UUID
    status: CategorySuggestionAnswer
    job_id: UUID | None = None
