"""The rules loader: rule rows to the effective RuleSet the engine runs (S14b T6; S14a hand-offs).

`load_rules` issues three statements on the caller's unit of work: the rule rows for the scope
(platform plus own, disabled rows included), the dictionary revisions (C6) and the current
category template. It then checks every row and resolves the RuleSet (S14a `resolve_ruleset`).

It fails closed with `RuleLoadError`, never skipping a row, when:
- a stored row no longer validates as a wire `Rule`;
- a row belongs to another tenant (RLS and the repository already prevent it: defence in depth);
- the stored `subject` differs from the engine's `subject_of(rule_def)` (L6);
- a tenant row names an error code outside the engine's item codes (CD5);
- a stored regex (a `pattern` rule, or a regex `banned_terms` list) is refused by the engine's
  regex guard (`compile_rule_regex`), compiled once per load;
- the resolver refuses the rows (`RuleSetMismatchError`) or the RuleSet itself is invalid.

The message names the rule id and the reason class, never a stored value.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol
from uuid import UUID

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import Marketplace
from mkos_schemas.jobs import CategoryTemplate
from mkos_schemas.rules import BannedTerms, Pattern, RuleSet
from pydantic import ValidationError

from app.repositories.dictionary import DictionaryRevisionRepository
from app.repositories.rules import RuleRecord, RuleRepository
from app.repositories.templates import CategoryTemplateRepository
from app.services.rules_engine import RuleRow, RuleSetMismatchError, resolve_ruleset
from app.services.rules_engine.resolver import subject_of
from app.services.rules_engine.text import UnsafeRuleRegexError, compile_rule_regex

# CD5: the codes the engine itself emits per item. A tenant rule may only use these.
TENANT_RULE_CODES: Final[frozenset[ErrorCode]] = frozenset(
    {
        ErrorCode.FIELD_TOO_LONG,
        ErrorCode.FIELD_TOO_SHORT,
        ErrorCode.BANNED_TERM,
        ErrorCode.MISSING_ATTRIBUTE,
        ErrorCode.VALUE_NOT_ALLOWED,
        ErrorCode.CROSS_FIELD_MISMATCH,
        ErrorCode.UNSUPPORTED_CLAIM,
        ErrorCode.PATTERN_MISMATCH,
    }
)


class RuleLoadError(Exception):
    """The stored rules for a scope cannot be run safely; the run fails closed."""


class RulesUnitOfWork(Protocol):
    @property
    def rules(self) -> RuleRepository: ...

    @property
    def dictionary(self) -> DictionaryRevisionRepository: ...

    @property
    def templates(self) -> CategoryTemplateRepository: ...


@dataclass(frozen=True, slots=True)
class LoadedRules:
    ruleset: RuleSet
    template: CategoryTemplate | None
    template_id: UUID | None = None
    template_version: int | None = None


def _check_regexes(record: RuleRecord) -> None:
    rule_def = record.rule.rule_def
    if isinstance(rule_def, Pattern):
        compile_rule_regex(rule_def.regex, ignore_case="i" in rule_def.flags)
    elif isinstance(rule_def, BannedTerms) and rule_def.match == "regex":
        for term in rule_def.terms:
            compile_rule_regex(term, ignore_case=not rule_def.case_sensitive)


def check_records(records: Iterable[RuleRecord], *, tenant_id: UUID) -> list[RuleRow]:
    """The fail-closed row checks (module docstring), then the resolver's rows."""
    rows: list[RuleRow] = []
    for record in records:
        rule = record.rule
        if record.tenant_id not in (None, tenant_id) or rule.tenant_id != record.tenant_id:
            raise RuleLoadError(f"rule {rule.id}: another tenant's row")
        if record.subject != subject_of(rule.rule_def):
            raise RuleLoadError(f"rule {rule.id}: stored subject differs from its rule_def")
        if record.tenant_id is not None and rule.rule_def.error_code not in TENANT_RULE_CODES:
            raise RuleLoadError(f"rule {rule.id}: a tenant rule may not use this error code")
        try:
            _check_regexes(record)
        except UnsafeRuleRegexError:
            raise RuleLoadError(f"rule {rule.id}: the regex guard refuses its pattern") from None
        rows.append(RuleRow(rule=rule, revision=record.revision))
    return rows


async def load_rules(
    uow: RulesUnitOfWork,
    *,
    tenant_id: UUID,
    marketplace: Marketplace | str,
    category: str,
    now: datetime | None = None,
) -> LoadedRules:
    """The effective RuleSet and current template for (tenant, marketplace, category)."""
    market = Marketplace(marketplace)
    try:
        records = await uow.rules.list_for_resolution(market.value, category)
    except ValidationError:
        raise RuleLoadError("a stored rule no longer validates") from None
    rows = check_records(records, tenant_id=tenant_id)
    definitions, category_attributes = await uow.dictionary.revisions(category)
    current = await uow.templates.current(market.value, category)
    try:
        ruleset = resolve_ruleset(
            rows,
            marketplace=market,
            category=category,
            tenant_id=tenant_id,
            dictionary_revisions=(definitions, category_attributes),
            loaded_at=now or datetime.now(UTC),
        )
    except (RuleSetMismatchError, ValidationError) as exc:
        raise RuleLoadError(f"the rules cannot be resolved: {type(exc).__name__}") from None
    if current is None:
        return LoadedRules(ruleset=ruleset, template=None)
    return LoadedRules(
        ruleset=ruleset,
        template=current.template,
        template_id=current.id,
        template_version=current.version,
    )
