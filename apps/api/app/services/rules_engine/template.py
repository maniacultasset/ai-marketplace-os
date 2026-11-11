"""Template checks derived from the category template (DATABASE.md:526; CODE-PLAN T10).

Required keys (``required_attributes`` plus specs with ``requirement="required"``) missing give
``MISSING_ATTRIBUTE``; conditional keys missing while every ``when`` condition holds give
``MISSING_ATTRIBUTE`` (a conditional spec with no ``when`` is never required); an ``enum`` spec
value outside ``allowed_values`` and the ``value_map`` keys (either alone is the vocabulary:
DATABASE.md:526 "enum not mappable") gives ``VALUE_NOT_ALLOWED``. Items
carry ``rule_id`` None, error severity, and come in sorted key order.

Client feedback item 10: a ``recommended_attributes`` key that is neither required nor
conditional and is missing gives ``MISSING_ATTRIBUTE`` at **warning** severity, the one template
warning (``is_recommendation``). It never blocks: the status, issue and question paths skip it,
and the pipeline never sees it (``without_recommendations``), so nothing is filled without a fact.

``requirement_levels`` names the level of every template key: ``required``, ``conditional``,
``recommended`` (an optional key the template recommends), ``optional`` or ``unsupported``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.jobs import CategoryTemplate

from .report import Draft
from .text import condition_holds, fold, is_present

if TYPE_CHECKING:
    from .evaluators import Ctx


def _present(ctx: Ctx, key: str) -> tuple[bool, object]:
    found, value = ctx.lookup(f"attributes.{key}")
    return found and is_present(value), value


RequirementLevel = Literal["required", "conditional", "recommended", "optional", "unsupported"]


def _missing(key: str, severity: Severity = Severity.ERROR) -> Draft:
    return Draft(
        f"attributes.{key}", ErrorCode.MISSING_ATTRIBUTE, None, {"attribute": key}, None, severity
    )


def _required_keys(template: CategoryTemplate) -> set[str]:
    keys = set(template.required_attributes)
    return keys | {k for k, s in template.specs.items() if s.requirement == "required"}


def _conditional_keys(template: CategoryTemplate) -> set[str]:
    keys = set(template.conditional_attributes)
    return keys | {k for k, s in template.specs.items() if s.requirement == "conditional"}


def _unsupported_keys(template: CategoryTemplate) -> set[str]:
    keys = set(template.unsupported_attributes)
    return keys | {k for k, s in template.specs.items() if s.requirement == "unsupported"}


def requirement_levels(template: CategoryTemplate) -> dict[str, RequirementLevel]:
    """The level of every key the template names, in sorted key order (module docstring)."""
    required = _required_keys(template)
    conditional = _conditional_keys(template) - required
    unsupported = _unsupported_keys(template)
    recommended = set(template.recommended_attributes)
    keys = {*template.optional_attributes, *template.specs, *required, *conditional}
    keys |= unsupported | recommended
    levels: dict[str, RequirementLevel] = {}
    for key in sorted(keys):
        if key in required:
            levels[key] = "required"
        elif key in conditional:
            levels[key] = "conditional"
        elif key in unsupported:
            levels[key] = "unsupported"
        elif key in recommended:
            levels[key] = "recommended"
        else:
            levels[key] = "optional"
    return levels


def is_recommendation(item: ErrorItem) -> bool:
    """A missing recommended attribute: the template's own (no rule) warning MISSING_ATTRIBUTE."""
    return (
        item.error_code is ErrorCode.MISSING_ATTRIBUTE
        and item.severity is Severity.WARNING
        and item.rule_id is None
        and item.source is ErrorSource.RULES_ENGINE
    )


def without_recommendations(report: ErrorReport) -> ErrorReport:
    """The report minus its recommendations; the status is unchanged (they never block)."""
    kept = [item for item in report.errors if not is_recommendation(item)]
    if len(kept) == len(report.errors):
        return report
    return report.model_copy(update={"errors": kept})


def template_allowed(template: CategoryTemplate | None, attribute: str) -> list[str] | None:
    """``allowed_values`` plus the ``value_map`` keys of the template spec, or None (no check)."""
    if template is None or attribute not in template.specs:
        return None
    spec = template.specs[attribute]
    if spec.allowed_values is None and not spec.value_map:
        return None
    return [*(spec.allowed_values or []), *sorted(spec.value_map)]


def template_checks(ctx: Ctx) -> list[Draft]:
    template = ctx.template
    if template is None:
        return []
    specs = template.specs
    drafts: list[Draft] = []
    required = _required_keys(template)
    for key in sorted(required):
        if not _present(ctx, key)[0]:
            drafts.append(_missing(key))
    conditional = _conditional_keys(template)
    for key in sorted(conditional - required):
        when = specs[key].when if key in specs else []
        holds = bool(when) and all(
            condition_holds(c, ctx.listing, product=ctx.product, account=ctx.account) for c in when
        )
        if holds and not _present(ctx, key)[0]:
            drafts.append(_missing(key))
    recommended = set(template.recommended_attributes) - required - conditional
    recommended -= _unsupported_keys(template)
    for key in sorted(recommended):
        if not _present(ctx, key)[0]:
            drafts.append(_missing(key, Severity.WARNING))
    for key in sorted(specs):
        allowed = template_allowed(template, key)
        if specs[key].value_type != "enum" or allowed is None:
            continue
        present, value = _present(ctx, key)
        if not present:
            continue
        folded = {fold(a) for a in allowed}
        values = value if isinstance(value, list) else [value]
        if all(fold(str(v)) in folded for v in values):
            continue
        shown = " ".join(str(v) for v in values)
        drafts.append(
            Draft(
                f"attributes.{key}",
                ErrorCode.VALUE_NOT_ALLOWED,
                shown,
                {"value": shown, "attribute": key, "allowed_values": ", ".join(allowed)},
                None,
            )
        )
    return drafts
