"""`ProductFactsRepository.resolve(product_id)`: the resolved `ProductFacts` of one product
(DATABASE.md:1189; S18 T2).

Statements, in this order (a standalone product takes four):
1. the product row (live only); a variant also reads its style row, a style its live colourways
   and its **available** matrix cells (an unavailable size never reaches the copy);
2. the brand profile row (the brand defaults: parties and country of origin);
3. the parties the product or the brand default names (one `IN` query; skipped when none);
4. the dictionary: `is_critical` per key and `dictionary_version` = max `revision`.

Priority per key: variant → colourway → style → brand default (for the keys whose levels include
the brand). Another tenant's or a soft-deleted product is None.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import func, select

from app.core.resolved_facts import (
    CellFacts,
    Classification,
    Commercial,
    Compliance,
    FactLevel,
    FamilySummary,
    Identifier,
    Identity,
    Package,
    PartyFacts,
    ProductFacts,
    ResolvedFact,
)
from app.db.models.catalog import Product, ProductColourway, VariantMatrixCell
from app.db.models.facts import BrandProfile, BusinessParty
from app.db.models.platform import AttributeDefinition
from app.repositories.base import TenantScopedRepository

PARTY_COLUMNS: Final = {
    "manufacturer": ("manufacturer_party_id", "default_manufacturer_party_id"),
    "marketer": ("marketer_party_id", "default_marketer_party_id"),
    "packer": ("packer_party_id", "default_packer_party_id"),
    "importer": ("importer_party_id", "default_importer_party_id"),
    "consumer_care": ("consumer_care_party_id", "consumer_care_party_id"),
}


class _Colourways(TenantScopedRepository[ProductColourway]):
    model = ProductColourway


class _Cells(TenantScopedRepository[VariantMatrixCell]):
    model = VariantMatrixCell


class _Brand(TenantScopedRepository[BrandProfile]):
    model = BrandProfile


class _Parties(TenantScopedRepository[BusinessParty]):
    model = BusinessParty


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _pick(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _level(product: Product) -> str:
    if product.parent_product_id is not None:
        return "variant"
    if product.variation_theme is not None:
        return "style"
    return "standalone"


def _address(party: BusinessParty) -> str | None:
    parts = [
        party.address_line1,
        party.address_line2,
        party.city,
        party.state_code,
        party.postal_code,
        party.country_code,
    ]
    shown = [p for p in parts if p]
    return ", ".join(shown) if shown else None


def _party(party: BusinessParty | None) -> PartyFacts | None:
    if party is None:
        return None
    return PartyFacts(
        legal_name=party.legal_name,
        address=_address(party),
        email=party.email,
        phone=party.phone,
        gstin=party.gstin,
    )


def _facts(
    layers: Sequence[tuple[FactLevel | None, Mapping[str, Any]]], critical: Mapping[str, bool]
) -> dict[str, ResolvedFact]:
    """Merge fact maps, the first layer winning; `inherited_from` names the layer a key came from
    (None: the product's own level)."""
    out: dict[str, ResolvedFact] = {}
    for level, facts in layers:
        for key, fact in sorted((facts or {}).items()):
            if key in out or not isinstance(fact, Mapping):
                continue
            out[key] = ResolvedFact.model_validate(
                {
                    **fact,
                    "key": key,
                    "inherited_from": level,
                    "is_critical": critical.get(key, False),
                }
            )
    return out


class ProductFactsRepository(TenantScopedRepository[Product]):
    model = Product

    async def _live(self, product_id: uuid.UUID) -> Product | None:
        stmt = self._select().where(Product.id == product_id, Product.deleted_at.is_(None))
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def _family(self, style: Product) -> FamilySummary:
        colourways = list(
            (
                await self.db.execute(
                    _Colourways(self.db, self.tenant_id)
                    ._select()
                    .where(
                        ProductColourway.style_product_id == style.id,
                        ProductColourway.deleted_at.is_(None),
                    )
                    .order_by(ProductColourway.position, ProductColourway.id)
                )
            ).scalars()
        )
        colour_of = {c.id: c.colour_key for c in colourways}
        cells = (
            await self.db.execute(
                _Cells(self.db, self.tenant_id)
                ._select()
                .where(
                    VariantMatrixCell.style_product_id == style.id,
                    VariantMatrixCell.availability == "available",
                )
                .order_by(VariantMatrixCell.colourway_id, VariantMatrixCell.size_key)
            )
        ).scalars()
        return FamilySummary(
            theme=style.variation_theme,
            colours=[c.colour_key for c in colourways if c.colour_key],
            size_run=list(style.size_run or []),
            cells=[
                CellFacts(
                    colour_key=colour_of.get(cell.colourway_id) if cell.colourway_id else None,
                    size_key=cell.size_key,
                    variant_product_id=cell.variant_product_id,
                )
                for cell in cells
            ],
        )

    async def _dictionary(self) -> tuple[dict[str, bool], int]:
        rows = (
            await self.db.execute(
                select(
                    AttributeDefinition.key,
                    AttributeDefinition.is_critical,
                    func.max(AttributeDefinition.revision).over(),
                )
            )
        ).all()
        version = int(rows[0][2]) if rows else 0
        return {row.key: bool(row.is_critical) for row in rows}, version

    async def resolve(self, product_id: uuid.UUID) -> ProductFacts | None:
        product = await self._live(product_id)
        if product is None:
            return None
        level = _level(product)
        style: Product | None = None
        family: FamilySummary | None = None
        if level == "variant" and product.parent_product_id is not None:
            style = await self._live(product.parent_product_id)
        elif level == "style":
            family = await self._family(product)
        brand = (
            await self.db.execute(_Brand(self.db, self.tenant_id)._select())
        ).scalar_one_or_none()

        def typed(name: str) -> Any:
            own = getattr(product, name)
            return _pick(own, None if style is None else getattr(style, name))

        party_ids: dict[str, uuid.UUID | None] = {}
        for role, (column, default) in PARTY_COLUMNS.items():
            party_ids[role] = _pick(typed(column), getattr(brand, default, None))
        wanted = sorted({p for p in party_ids.values() if p is not None})
        parties: dict[uuid.UUID, BusinessParty] = {}
        if wanted:
            rows = (
                await self.db.execute(
                    _Parties(self.db, self.tenant_id)
                    ._select()
                    .where(BusinessParty.id.in_(wanted), BusinessParty.deleted_at.is_(None))
                )
            ).scalars()
            parties = {p.id: p for p in rows}
        critical, version = await self._dictionary()

        def party(role: str) -> PartyFacts | None:
            found = party_ids[role]
            return None if found is None else _party(parties.get(found))

        layers: list[tuple[FactLevel | None, Mapping[str, Any]]] = [(None, product.attributes)]
        provenance: list[tuple[FactLevel | None, Mapping[str, Any]]] = [(None, product.provenance)]
        if style is not None:
            layers.append(("style", style.attributes))
            provenance.append(("style", style.provenance))
        if brand is not None:
            provenance.append(("brand", brand.provenance))
        variant_attrs = product.variant_attributes or {}
        return ProductFacts(
            product_id=product.id,
            sku=product.sku,
            level=level,
            style_product_id=style.id if style is not None else None,
            colour_key=variant_attrs.get("color"),
            size_key=variant_attrs.get("size"),
            identity=Identity(
                title=typed("title"),
                brand_name=typed("brand_name"),
                style_number=typed("style_number"),
                gender=typed("gender"),
                product_type=typed("product_type"),
            ),
            classification=Classification(
                category=typed("category"), sub_category=typed("sub_category")
            ),
            commercial=Commercial(
                mrp=_dec(typed("mrp")),
                selling_price=_dec(typed("selling_price")),
                currency=typed("currency"),
                tax_rate=_dec(typed("tax_rate")),
                hsn_code=typed("hsn_code"),
            ),
            identifier=Identifier(
                type=product.identifier_type,
                value=product.identifier_value,
                status=product.identifier_status,
            ),
            package=Package(
                length_cm=_dec(typed("package_length_cm")),
                breadth_cm=_dec(typed("package_breadth_cm")),
                height_cm=_dec(typed("package_height_cm")),
                weight_g=typed("package_weight_g"),
            ),
            product_weight_g=typed("product_weight_g"),
            compliance=Compliance(
                country_of_origin=_pick(
                    typed("country_of_origin"), getattr(brand, "default_country_of_origin", None)
                ),
                generic_name=typed("generic_name"),
                net_quantity=_dec(typed("net_quantity")),
                unit=typed("net_quantity_unit"),
                manufacturer=party("manufacturer"),
                marketer=party("marketer"),
                packer=party("packer"),
                importer=party("importer"),
                consumer_care=party("consumer_care"),
            ),
            attributes=_facts(layers, critical),
            provenance=_facts(provenance, critical),
            family=family,
            dictionary_version=version,
        )
