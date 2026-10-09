"""`ProductFacts`: the resolved read model of one product (DATABASE.md:1189-1192; S18 D5).

Pure: no I/O. These are internal pydantic models, not a contract (`mkos_schemas/facts.py` has no v1
owner): `GenerationContext.product` stays a `dict` and gets `for_prompt(facts)`.

- `ProductFacts` carries every DATABASE.md:1189 field, so `facts_digest` is final.
- `for_prompt(facts)`: what the AI sees. Every party is reduced to its `legal_name` (a nameless
  party is dropped), `gstin` never appears, and blank values, `provenance` evidence and the
  dictionary version are left out.
- `for_rules(facts)`: the mapping the Rules Engine's `product.*` paths read, and the only facts the
  pipeline deps object holds (CP13). Parties are `{legal_name}`; no contact, GSTIN or evidence.
- `facts_digest(facts)`: sha256 of the canonical JSON (sorted keys, Decimals as strings, every
  `updated_at` dropped) of the full facts, parties, family, provenance and dictionary version
  included: it pins the facts a validation run checked.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CellFacts",
    "Classification",
    "Commercial",
    "Compliance",
    "FactLevel",
    "FamilySummary",
    "Identifier",
    "Identity",
    "Package",
    "PartyFacts",
    "ProductFacts",
    "ResolvedFact",
    "facts_digest",
    "for_prompt",
    "for_rules",
]

FactLevel = Literal["brand", "style", "colour", "variant", "standalone"]
ProductLevel = Literal["standalone", "style", "variant"]
PARTY_ROLES: Final = ("manufacturer", "marketer", "packer", "importer", "consumer_care")
RULES_KEYS: Final = (
    "product_id",
    "sku",
    "level",
    "style_product_id",
    "colour_key",
    "size_key",
    "identity",
    "classification",
    "commercial",
    "identifier",
    "package",
    "product_weight_g",
    "compliance",
    "attributes",
    "family",
)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class ResolvedFact(_Model):
    """A stored `Fact` (DATABASE.md:1084-1094) plus where it was resolved from."""

    key: str
    value: Any = None
    normalized: Any = None
    source: str | None = None
    captured_via: str | None = None
    normalized_by: str | None = None
    confidence: float | None = None
    evidence_ref: str | None = None
    evidence_snapshot: dict[str, Any] | None = None
    agent_run_id: str | None = None
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    updated_at: str | None = None
    inherited_from: FactLevel | None = None
    is_critical: bool = False

    @property
    def resolved(self) -> Any:
        """The value a reader uses: the normalized one when present."""
        return self.normalized if self.normalized is not None else self.value


class PartyFacts(_Model):
    legal_name: str | None = None
    address: str | None = None
    email: str | None = None
    phone: str | None = None
    gstin: str | None = None


class CellFacts(_Model):
    colour_key: str | None = None
    size_key: str | None = None
    variant_product_id: UUID | None = None


class FamilySummary(_Model):
    theme: str | None = None
    colours: list[str] = Field(default_factory=list)
    size_run: list[str] = Field(default_factory=list)
    cells: list[CellFacts] = Field(default_factory=list)


class Identity(_Model):
    title: str | None = None
    brand_name: str | None = None
    style_number: str | None = None
    gender: str | None = None
    product_type: str | None = None


class Classification(_Model):
    category: str | None = None
    sub_category: str | None = None


class Commercial(_Model):
    """Decimals travel as strings (`"1.10"` stays `"1.10"`)."""

    mrp: str | None = None
    selling_price: str | None = None
    currency: str | None = None
    tax_rate: str | None = None
    hsn_code: str | None = None


class Identifier(_Model):
    type: str | None = None
    value: str | None = None
    status: str | None = None


class Package(_Model):
    length_cm: str | None = None
    breadth_cm: str | None = None
    height_cm: str | None = None
    weight_g: int | None = None


class Compliance(_Model):
    country_of_origin: str | None = None
    generic_name: str | None = None
    net_quantity: str | None = None
    unit: str | None = None
    manufacturer: PartyFacts | None = None
    marketer: PartyFacts | None = None
    packer: PartyFacts | None = None
    importer: PartyFacts | None = None
    consumer_care: PartyFacts | None = None


class ProductFacts(_Model):
    product_id: UUID
    sku: str
    level: ProductLevel
    style_product_id: UUID | None = None
    colour_key: str | None = None
    size_key: str | None = None
    identity: Identity = Field(default_factory=Identity)
    classification: Classification = Field(default_factory=Classification)
    commercial: Commercial = Field(default_factory=Commercial)
    identifier: Identifier = Field(default_factory=Identifier)
    package: Package = Field(default_factory=Package)
    product_weight_g: int | None = None
    compliance: Compliance = Field(default_factory=Compliance)
    attributes: dict[str, ResolvedFact] = Field(default_factory=dict)
    provenance: dict[str, ResolvedFact] = Field(default_factory=dict)
    family: FamilySummary | None = None
    dictionary_version: int = 0


# ---- projections ----


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _prune(value: Any) -> Any:
    """Drop None and blank strings (and the containers they empty) from a JSON-shaped value."""
    if isinstance(value, dict):
        out = {k: _prune(v) for k, v in value.items() if not _blank(v)}
        return {k: v for k, v in out.items() if v != {} and v != []}
    if isinstance(value, list):
        return [_prune(v) for v in value if not _blank(v)]
    return value


def _party_name(party: PartyFacts | None) -> dict[str, str] | None:
    if party is None or _blank(party.legal_name):
        return None
    return {"legal_name": str(party.legal_name)}


def _compliance_names(compliance: Compliance) -> dict[str, Any]:
    out: dict[str, Any] = compliance.model_dump(mode="json", exclude=set(PARTY_ROLES))
    for role in PARTY_ROLES:
        out[role] = _party_name(getattr(compliance, role))
    return out


def _attribute_values(facts: ProductFacts) -> dict[str, Any]:
    return {key: fact.resolved for key, fact in sorted(facts.attributes.items())}


def for_prompt(facts: ProductFacts) -> dict[str, Any]:
    """The `GenerationContext.product` dict (module docstring)."""
    body: dict[str, Any] = {
        "product_id": str(facts.product_id),
        "sku": facts.sku,
        "level": facts.level,
        "style_product_id": None if facts.style_product_id is None else str(facts.style_product_id),
        "colour_key": facts.colour_key,
        "size_key": facts.size_key,
        "identity": facts.identity.model_dump(mode="json"),
        "classification": facts.classification.model_dump(mode="json"),
        "commercial": facts.commercial.model_dump(mode="json"),
        "identifier": facts.identifier.model_dump(mode="json"),
        "package": facts.package.model_dump(mode="json"),
        "product_weight_g": facts.product_weight_g,
        "compliance": _compliance_names(facts.compliance),
        "attributes": _attribute_values(facts),
        "family": None if facts.family is None else facts.family.model_dump(mode="json"),
    }
    pruned: dict[str, Any] = _prune(body)
    return pruned


def for_rules(facts: ProductFacts) -> dict[str, Any]:
    """The Rules Engine's `product` mapping; exactly `RULES_KEYS` at the top level."""
    return {
        "product_id": str(facts.product_id),
        "sku": facts.sku,
        "level": facts.level,
        "style_product_id": None if facts.style_product_id is None else str(facts.style_product_id),
        "colour_key": facts.colour_key,
        "size_key": facts.size_key,
        "identity": facts.identity.model_dump(mode="json"),
        "classification": facts.classification.model_dump(mode="json"),
        "commercial": facts.commercial.model_dump(mode="json"),
        "identifier": facts.identifier.model_dump(mode="json"),
        "package": facts.package.model_dump(mode="json"),
        "product_weight_g": facts.product_weight_g,
        "compliance": _compliance_names(facts.compliance),
        "attributes": _attribute_values(facts),
        "family": None if facts.family is None else facts.family.model_dump(mode="json"),
    }


def _drop_updated_at(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_updated_at(v) for k, v in value.items() if k != "updated_at"}
    if isinstance(value, list):
        return [_drop_updated_at(v) for v in value]
    return value


def canonical_facts_json(facts: ProductFacts) -> bytes:
    body = _drop_updated_at(facts.model_dump(mode="json"))
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def facts_digest(facts: ProductFacts) -> bytes:
    """sha256 of `canonical_facts_json(facts)` (32 bytes, `validation_runs.facts_digest`)."""
    return hashlib.sha256(canonical_facts_json(facts)).digest()
