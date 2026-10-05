"""``RuleSetResolver``: rule rows to the effective RuleSet (DATABASE.md:507; A§17; T55).

Rows group by ``(tenant_id, field, rule_type, subject, pack)`` (DATABASE.md:498 subject). In each
group the category rows are used if any is enabled, else the category=NULL rows; the highest
enabled version wins; none enabled means the rule is off. Tenant rules never shadow platform rules
(the tenant id is part of the key), so they can only tighten. ``rules_version`` is the highest
revision over every fetched row (disabled ones included) and every dictionary revision.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from mkos_schemas.enums import Marketplace
from mkos_schemas.rules import (
    AllowedValues,
    ClaimRequiresAttribute,
    ConditionalRequired,
    CrossField,
    FactBackedAttribute,
    FactSource,
    ImageRequirement,
    Pattern,
    RequiredAttribute,
    Rule,
    RuleDef,
    RuleSet,
)

from .report import RuleSetMismatchError

GroupKey = tuple[str, str, str, str, str]


@dataclass(frozen=True)
class RuleRow:
    """One fetched ``rules`` row: the rule and its row ``revision``."""

    rule: Rule
    revision: int

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("revision must be >= 0")


def subject_of(rule_def: RuleDef) -> str:
    """The ``rules_subject_matches`` subject (DATABASE.md:498), ``''`` when the type has none."""
    if isinstance(
        rule_def,
        RequiredAttribute | AllowedValues | ConditionalRequired | FactSource | FactBackedAttribute,
    ):
        return rule_def.attribute
    if isinstance(rule_def, ClaimRequiresAttribute):
        return rule_def.claim
    if isinstance(rule_def, CrossField | Pattern):
        return rule_def.name
    if isinstance(rule_def, ImageRequirement):
        return f"{rule_def.slot}:{rule_def.check}"
    return ""


def _key(rule: Rule) -> GroupKey:
    tenant = "" if rule.tenant_id is None else str(rule.tenant_id)
    rule_def = rule.rule_def
    return (tenant, rule.field, rule_def.type, subject_of(rule_def), rule_def.pack)


def _winner(rows: list[RuleRow]) -> Rule | None:
    enabled = [r for r in rows if r.rule.enabled]
    if not enabled:
        return None
    return max(enabled, key=lambda r: (r.rule.version, r.revision, str(r.rule.id))).rule


def _order(rule: Rule) -> tuple[bool, str, str, str, str, str]:
    rule_def = rule.rule_def
    return (
        rule.tenant_id is not None,
        rule.field,
        rule_def.type,
        subject_of(rule_def),
        rule_def.pack,
        str(rule.id),
    )


def resolve_ruleset(
    rows: Iterable[RuleRow],
    *,
    marketplace: Marketplace,
    category: str,
    tenant_id: UUID | None,
    dictionary_revisions: Iterable[int] = (),
    loaded_at: datetime,
) -> RuleSet:
    """The effective RuleSet for one (marketplace, category, tenant); see the module docstring.

    Raises ``RuleSetMismatchError`` for a row of another marketplace, another non-NULL category or
    another tenant, and a validation error above 500 effective rules.
    """
    rows = list(rows)
    groups: dict[GroupKey, list[RuleRow]] = {}
    for row in rows:
        rule = row.rule
        if rule.marketplace is not marketplace:
            raise RuleSetMismatchError(f"rule {rule.id} belongs to another marketplace")
        if rule.category is not None and rule.category != category:
            raise RuleSetMismatchError(f"rule {rule.id} belongs to another category")
        if rule.tenant_id is not None and rule.tenant_id != tenant_id:
            raise RuleSetMismatchError(f"rule {rule.id} belongs to another tenant")
        groups.setdefault(_key(rule), []).append(row)
    effective: list[Rule] = []
    for group in groups.values():
        category_rows = [r for r in group if r.rule.category is not None]
        null_rows = [r for r in group if r.rule.category is None]
        winner = _winner(category_rows) or _winner(null_rows)
        if winner is not None:
            effective.append(winner)
    effective.sort(key=_order)
    revisions = [r.revision for r in rows] + list(dictionary_revisions)
    return RuleSet.model_validate(
        {
            "marketplace": marketplace,
            "category": category,
            "rules": effective,
            "rules_version": max(revisions, default=0),
            "loaded_at": loaded_at,
        }
    )
