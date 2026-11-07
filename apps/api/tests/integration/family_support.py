"""A complete variant family for adapter tests (S20): a `size_color` style with a size run, one
colourway per colour and one available matrix cell per child, written in one transaction so the
deferred matrix check (0004 `variant_matrix_complete`) sees the whole family at commit."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from . import factories as f

Conn = Any

INSERT_CELL = (
    "INSERT INTO variant_matrix_cells (tenant_id, style_product_id, colourway_id, size_key, "
    "availability, variant_product_id, source) VALUES (%s, %s, %s, %s, 'available', %s, 'seller')"
)


def family(
    conn: Conn,
    tenant_id: UUID,
    sku: str,
    *,
    sizes: Sequence[str] = ("s", "m"),
    colours: Sequence[str] = ("navy blue", "maroon"),
    **style_columns: Any,
) -> tuple[UUID, dict[str, UUID]]:
    """(style id, {child sku: child id}); child SKUs are `<sku>-<SIZE>-<n>` per colour n."""
    # SI2 T2: a priced style (with the seller's fabric) the children inherit from, and the
    # tenant's brand-default manufacturer, unless the caller says otherwise
    priced = {
        **f.SELLER_PRICED,
        "attributes": {"fabric_type": f.fact(value="cotton")},
        "gender": "women",  # SI8: Amazon apparel needs the audience
    }
    style_columns = {**priced, **style_columns}
    f.ensure_brand_manufacturer(conn, tenant_id)
    with conn.transaction():
        style = f.product(conn, tenant_id, "style", sku=sku, size_run=list(sizes), **style_columns)
        children: dict[str, UUID] = {}
        for n, colour in enumerate(colours, start=1):
            colourway = f.colourway(
                conn, tenant_id, style, attributes={"color": f.fact(value=colour)}, position=n
            )
            for size in sizes:
                child_sku = f"{sku}-{size.upper()}-{n}"
                child = f.product(
                    conn,
                    tenant_id,
                    "child",
                    parent=style,
                    sku=child_sku,
                    category=style_columns.get("category", "t_shirts"),
                    variant_attributes={"size": size, "color": colour},
                    attributes={"size": f.fact(value=size.upper(), normalized=size)},
                )
                conn.execute(INSERT_CELL, (tenant_id, style, colourway, size, child))
                children[child_sku] = child
    return style, children
