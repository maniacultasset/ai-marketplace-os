"""Template checks derived from the category template (DATABASE.md:526; CODE-PLAN T10).

Required keys (``required_attributes`` plus specs with ``requirement="required"``) missing give
``MISSING_ATTRIBUTE``; conditional keys missing while every ``when`` condition holds give
``MISSING_ATTRIBUTE`` (a conditional spec with no ``when`` is never required); an ``enum`` spec
value outside ``allowed_values`` and the ``value_map`` keys gives ``VALUE_NOT_ALLOWED``. Items
carry ``rule_id`` None, error severity, and come in sorted key order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mkos_schemas.codes import ErrorCode

from .report import Draft
from .text import condition_holds, fold, is_present

if TYPE_CHECKING:
    from .evaluators import Ctx


def _present(ctx: Ctx, key: str) -> tuple[bool, object]:
    found, value = ctx.lookup(f"attributes.{key}")
    return found and is_present(value), value


def _missing(key: str) -> Draft:
    return Draft(f"attributes.{key}", ErrorCode.MISSING_ATTRIBUTE, None, {"attribute": key}, None)


def template_checks(ctx: Ctx) -> list[Draft]:
    template = ctx.template
    if template is None:
        return []
    specs = template.specs
    drafts: list[Draft] = []
    required = set(template.required_attributes)
    required |= {k for k, s in specs.items() if s.requirement == "required"}
    for key in sorted(required):
        if not _present(ctx, key)[0]:
            drafts.append(_missing(key))
    conditional = set(template.conditional_attributes)
    conditional |= {k for k, s in specs.items() if s.requirement == "conditional"}
    for key in sorted(conditional - required):
        when = specs[key].when if key in specs else []
        holds = bool(when) and all(
            condition_holds(c, ctx.listing, product=ctx.product, account=ctx.account) for c in when
        )
        if holds and not _present(ctx, key)[0]:
            drafts.append(_missing(key))
    for key in sorted(specs):
        spec = specs[key]
        if spec.value_type != "enum" or spec.allowed_values is None:
            continue
        present, value = _present(ctx, key)
        if not present:
            continue
        allowed = [*spec.allowed_values, *sorted(spec.value_map)]
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
