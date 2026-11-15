"""G2: the answer of the import-wide photo link retry."""

from __future__ import annotations

from typing import Annotated

from mkos_schemas.base import WireModel
from pydantic import Field


class PhotoLinksRetryOut(WireModel):
    """The links put back in the download queue, and the products whose download job was queued
    (poll the import report or each product for the results)."""

    queued: Annotated[int, Field(ge=1)]
    products: Annotated[int, Field(ge=1)]
