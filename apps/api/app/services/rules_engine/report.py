"""Error items and report shaping for the Rules Engine (S14a CODE-PLAN T4; D10g, D13, D14).

Message placeholders (DATABASE.md:510), substituted in rule ``message`` and ``suggestion``:

- ``{value}``: for ``max_length``/``min_length`` the measured length; for ``max_items``/
  ``min_items`` the item count; for every other rule the offending value.
- ``{limit}``: the rule's limit. ``{term}``: the matched terms, in rule order, joined by ", ".
- ``{attribute}``: the attribute key. ``{allowed_values}``: the allowed values joined by ", ".
- ``{slot}`` and ``{threshold}``: image rules (not evaluated in S14a); they render "".

A placeholder that does not apply renders "". Only a bare placeholder name is substituted: a
format spec, a conversion, an attribute or index access, an unknown name or unbalanced braces stay
literal text, so a rule can never make the engine allocate or evaluate anything. Each value is cut
to 200 characters and the rendered text to 1000 (the ``ErrorItem.message`` cap); a blank result
falls back to the unrendered template.

Template checks (``rule_id`` None) use fixed English messages:

- ``MISSING_ATTRIBUTE``: "Required attribute {attribute} is missing".
- ``VALUE_NOT_ALLOWED``: "Value {value} is not allowed for {attribute}; allowed: {allowed_values}".
"""

from __future__ import annotations

import re
import string
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, Marketplace, RetryClass, Severity
from mkos_schemas.errors import MAX_ERROR_ITEMS, ErrorItem, ErrorReport
from mkos_schemas.rules import PLACEHOLDERS, Rule

PLACEHOLDER_VALUE_MAX: Final = 200
TEXT_MAX: Final = 1000
TEMPLATE_MESSAGES: Final[Mapping[ErrorCode, str]] = {
    ErrorCode.MISSING_ATTRIBUTE: "Required attribute {attribute} is missing",
    ErrorCode.VALUE_NOT_ALLOWED: (
        "Value {value} is not allowed for {attribute}; allowed: {allowed_values}"
    ),
}
_RETRYABLE: Final = frozenset({RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT})


class RuleSetMismatchError(ValueError):
    """The listing, a rule or the template belongs to another marketplace, category or tenant."""


@dataclass(frozen=True)
class Draft:
    """One finding before it becomes an ``ErrorItem``; ``rule`` is None for template checks."""

    field: str
    code: ErrorCode
    value: object
    placeholders: Mapping[str, str]
    rule: Rule | None
    severity: Severity = Severity.ERROR


def render_text(template: str, values: Mapping[str, str]) -> str:
    """Substitute bare ``{name}`` placeholders; everything else stays literal. Never raises."""
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError:
        return template[:TEXT_MAX]
    out: list[str] = []
    for literal, name, spec, conversion in parsed:
        out.append(literal)
        if name is None:
            continue
        if name in PLACEHOLDERS and not spec and conversion is None:
            out.append(values.get(name, "")[:PLACEHOLDER_VALUE_MAX])
            continue
        source = name
        if conversion is not None:
            source += f"!{conversion}"
        if spec:
            source += f":{spec}"
        out.append("{" + source + "}")
    rendered = "".join(out)[:TEXT_MAX]
    if not rendered.strip():
        return template[:TEXT_MAX]
    return rendered


def submitted_text(value: object) -> str | None:
    """The ``value_submitted`` text: a list is joined by one space."""
    if value is None:
        return None
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value)


def make_item(draft: Draft, *, marketplace: Marketplace) -> ErrorItem:
    rule = draft.rule
    meta = ERROR_CODE_META[draft.code]
    if rule is None:
        message = render_text(TEMPLATE_MESSAGES[draft.code], draft.placeholders)
        suggestion: str | None = None
        error_class = meta.error_class
        retry_class = meta.retry_class
    else:
        rule_def = rule.rule_def
        message = render_text(rule_def.message, draft.placeholders)
        suggestion = (
            None
            if rule_def.suggestion is None
            else render_text(rule_def.suggestion, draft.placeholders)
        )
        error_class = rule_def.error_class or meta.error_class
        retry_class = rule_def.retry_class or meta.retry_class
    return ErrorItem(
        field=draft.field,
        error_code=draft.code,
        severity=draft.severity,
        message=message,
        value_submitted=submitted_text(draft.value),  # ErrorItem redacts secrets and caps it
        suggestion=suggestion,
        source=ErrorSource.RULES_ENGINE,
        retryable=retry_class in _RETRYABLE,
        marketplace=marketplace,
        rule_id=None if rule is None else rule.id,
        error_class=error_class,
        retry_class=retry_class,
    )


_FAMILIES: Final = (
    ("content.title", 0),
    ("content.bullets", 1),
    ("content.description", 2),
    ("keywords.search_terms", 3),
    ("keywords.hidden_terms", 4),
    ("attributes", 5),
    ("images", 6),
    ("facts", 7),
    ("variants", 8),
    ("brand", 9),
    ("listing", 10),
    ("request", 11),
    ("file", 12),
)
_INDEX = re.compile(r"\[([0-9]+)\]")


def path_rank(path: str) -> tuple[int, int, str, str]:
    """A total order over field paths: family, numeric index, attribute key, the path itself."""
    family = 99
    rest = ""
    for prefix, rank in _FAMILIES:
        if path == prefix or path.startswith((prefix + ".", prefix + "[")):
            family = rank
            rest = path[len(prefix) :]
            break
    index_match = _INDEX.match(rest)
    index = int(index_match.group(1)) if index_match else -1
    key = rest[1:] if rest.startswith(".") else ""
    return family, index, key, path


def _is_platform(draft: Draft) -> bool:
    return draft.rule is None or draft.rule.tenant_id is None


def _sort_key(pair: tuple[Draft, ErrorItem]) -> tuple[object, ...]:
    draft, item = pair
    rule_id: UUID | None = item.rule_id
    return (
        item.severity is not Severity.ERROR,
        not _is_platform(draft),
        path_rank(item.field),
        item.error_code.value,
        rule_id is None,
        "" if rule_id is None else str(rule_id),
        item.message,
    )


def _dedupe(drafts: Iterable[Draft]) -> list[Draft]:
    seen: set[tuple[str, ErrorCode, UUID | None]] = set()
    kept: list[Draft] = []
    for draft in drafts:
        key = (draft.field, draft.code, None if draft.rule is None else draft.rule.id)
        if key not in seen:
            seen.add(key)
            kept.append(draft)
    suppressing = {
        (d.field, d.code)
        for d in kept
        if d.rule is not None and d.rule.tenant_id is None and d.severity is Severity.ERROR
    }
    return [d for d in kept if d.rule is not None or (d.field, d.code) not in suppressing]


def build_report(
    drafts: Iterable[Draft], *, listing_id: UUID | None, marketplace: Marketplace
) -> ErrorReport:
    """Dedupe, derive the status from the full set, sort (D13) and cap at 500."""
    pairs = [(d, make_item(d, marketplace=marketplace)) for d in _dedupe(drafts)]
    blocking = [item for _, item in pairs if item.severity is Severity.ERROR]
    failed = bool(blocking)
    pairs.sort(key=_sort_key)
    return ErrorReport(
        status="failed" if failed else "passed",
        error_code=ErrorCode.VALIDATION_FAILED if failed else None,
        retryable=failed and all(item.retryable for item in blocking),
        errors=[item for _, item in pairs[:MAX_ERROR_ITEMS]],
        listing_id=listing_id,
    )
