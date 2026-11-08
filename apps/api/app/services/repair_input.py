"""SI11 (T10): the stored repairs as the adapter's input. Pure: rows in, `AdapterRepair`s out."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from app.adapters.base import AdapterRepair


def adapter_repairs(
    rows: Iterable[Any], sku_by_item: Mapping[uuid.UUID, str] | None = None
) -> tuple[AdapterRepair, ...]:
    """Each live `listing_repairs` row as an `AdapterRepair`; a row naming a listing item the
    map does not hold applies to no unit (its SKU is unknown here), so it is left out."""
    out: list[AdapterRepair] = []
    for row in rows:
        sku: str | None = None
        if row.listing_item_id is not None:
            sku = (sku_by_item or {}).get(row.listing_item_id)
            if sku is None:
                continue
        value = row.value_json
        out.append(
            AdapterRepair(
                id=row.id,
                field_path=row.field_path,
                attribute=row.attribute,
                rule=row.rule,
                source_digest=bytes(row.source_digest),
                to_digest=bytes(row.to_digest),
                value=None if value is None else tuple(value),
                seller_sku=sku,
            )
        )
    return tuple(out)
