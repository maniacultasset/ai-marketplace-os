"""Rule definitions and rule sets (data, not code)."""

from __future__ import annotations

import importlib
import re
import string
from datetime import date
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .base import AttrKey, NonBlankStr, Slug, StrictModel, WireModel
from .codes import ErrorCode
from .enums import ErrorClass, Marketplace, RetryClass, Severity
from .paths import is_valid_condition_path, is_valid_field_path, is_valid_rule_field

RuleTerm = Annotated[NonBlankStr, Field(max_length=256)]
"""A banned term, claim term or allowed claim value: 1..256 characters (Appendix B)."""

PLACEHOLDERS: frozenset[str] = frozenset(
    {"value", "limit", "term", "attribute", "allowed_values", "slot", "threshold"}
)

MAX_RULESET_RULES: int = 500
# The stdlib regex parser (no public API; typeshed has no stubs for it). Python is pinned ==3.13.*.
_sre: Any = importlib.import_module("re._constants")
_sre_parser: Any = importlib.import_module("re._parser")
_QUANT_AFTER_GROUP = re.compile(r"\+|\*|\{(\d*)(,?)(\d*)\}")
_REPEATS: Final = frozenset({_sre.MAX_REPEAT, _sre.MIN_REPEAT})
# RE2 runs rule patterns (DATABASE.md pattern row): no lookaround, backreference, atomic group or
# possessive repeat.
_RE2_UNSUPPORTED: Final = frozenset(
    {
        _sre.ASSERT,
        _sre.ASSERT_NOT,
        _sre.GROUPREF,
        _sre.GROUPREF_EXISTS,
        _sre.ATOMIC_GROUP,
        _sre.POSSESSIVE_REPEAT,
    }
)


def _children(op: Any, av: Any) -> list[Any]:
    """Sub-patterns below one parse node (shapes from ``re._parser``)."""
    if op in _REPEATS or op is _sre.POSSESSIVE_REPEAT:
        return [av[2]]
    if op is _sre.SUBPATTERN:
        return [av[3]]
    if op is _sre.BRANCH:
        return list(av[1])
    if op in (_sre.ASSERT, _sre.ASSERT_NOT):
        return [av[1]]
    if op is _sre.ATOMIC_GROUP:
        return [av]
    if op is _sre.GROUPREF_EXISTS:
        return [x for x in av[1:] if x is not None]
    return []


def _has_variable_repeat_or_branch(sub: Any) -> bool:
    for op, av in sub:
        if op is _sre.BRANCH or (op in _REPEATS and av[0] != av[1]):
            return True  # a fixed count such as \d{3} cannot backtrack
        if any(_has_variable_repeat_or_branch(child) for child in _children(op, av)):
            return True
    return False


def _unsafe_node(sub: Any) -> str | None:
    for op, av in sub:
        if op in _RE2_UNSUPPORTED:
            return "lookaround, backreference, atomic group and possessive repeat are not RE2"
        if op in _REPEATS and av[1] > 1 and _has_variable_repeat_or_branch(av[2]):
            return "nested quantifier or alternation inside a repeated group"
        for child in _children(op, av):
            reason = _unsafe_node(child)
            if reason is not None:
                return reason
    return None


def _repeats_many(regex: str, pos: int) -> bool:
    m = _QUANT_AFTER_GROUP.match(regex, pos)
    if m is None:
        return False
    if m.group(0) in ("+", "*"):
        return True
    low, comma, high = m.groups()
    if comma:
        return high == "" or int(high) > 1
    return low != "" and int(low) > 1


def _alternation_group_repeated(regex: str) -> bool:
    """``(\\w|\\d)+``: the parser folds such a branch into a set, so check the source text."""
    bars: list[bool] = []
    i, n = 0, len(regex)
    while i < n:
        c = regex[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            i += 1
            if i < n and regex[i] == "^":
                i += 1
            if i < n and regex[i] == "]":
                i += 1
            while i < n and regex[i] != "]":
                i += 2 if regex[i] == "\\" else 1
            i += 1
            continue
        if c == "(":
            bars.append(False)
        elif c == "|" and bars:
            bars[-1] = True
        elif c == ")" and bars and bars.pop() and _repeats_many(regex, i + 1):
            return True
        i += 1
    return False


def check_rule_regex(regex: str) -> str:
    """Refuse a rule regex that does not compile, is not RE2, or can backtrack badly.

    Rejected: a group repeated more than once whose body holds a quantifier or an alternation
    (``(a+)+``, ``(a+){2,}``, ``(.*a){20}``, ``(a|aa)+``, ``(\\w|\\d)+``). Allowed: a single quantifier
    on an atom or set, and optional groups (``^\\d+(\\.\\d+)?$``, ``https?://.*``). The S14a engine
    still runs patterns in RE2 with a timeout; this is the contract-side gate.
    """
    try:
        tree = _sre_parser.parse(regex)
        reason = _unsafe_node(tree)
        if reason is None and _alternation_group_repeated(regex):
            reason = "nested quantifier or alternation inside a repeated group"
        if reason is None:
            re.compile(regex)
    except (re.error, OverflowError, RecursionError) as exc:
        raise ValueError(f"invalid regex: {exc}") from None
    if reason is not None:
        raise ValueError(reason)
    return regex


class PolicyRef(WireModel):
    source: Literal["amazon_policy", "flipkart_policy", "legal_metrology", "platform", "brand"]
    title: NonBlankStr
    url: str | None = None
    section: str | None = None
    retrieved_on: date | None = None


class Condition(WireModel):
    path: str
    op: Literal["eq", "ne", "in", "not_in", "present", "absent"]
    value: str | int | float | bool | None = None
    values: list[str | int | float | bool] | None = None

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not is_valid_condition_path(value):
            raise ValueError("illegal condition path")
        return value

    @model_validator(mode="after")
    def _op_values(self) -> Condition:
        if self.op in {"in", "not_in"}:
            if not self.values:
                raise ValueError("values required")
            if self.value is not None:
                raise ValueError("value must be absent for in/not_in")
        elif self.op in {"eq", "ne"}:
            if self.value is None:
                raise ValueError("value required")
            if self.values:
                raise ValueError("values must be absent")
        else:
            if self.value is not None or self.values:
                raise ValueError("present/absent take no value")
        return self


class ImageRequirementParams(StrictModel):
    min_px: int | None = Field(default=None, gt=0)
    max_px: int | None = Field(default=None, gt=0)
    ratio_w: int | None = Field(default=None, gt=0)
    ratio_h: int | None = Field(default=None, gt=0)
    min_fill: float | None = Field(default=None, ge=0.0, le=1.0)
    max_bytes: int | None = Field(default=None, gt=0)
    allowed_formats: Annotated[list[NonBlankStr], Field(max_length=8)] = Field(default_factory=list)
    rgb: (
        tuple[
            Annotated[int, Field(ge=0, le=255)],
            Annotated[int, Field(ge=0, le=255)],
            Annotated[int, Field(ge=0, le=255)],
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def _at_least_one(self) -> ImageRequirementParams:
        filled = any(
            (
                self.min_px is not None,
                self.max_px is not None,
                self.ratio_w is not None,
                self.ratio_h is not None,
                self.min_fill is not None,
                self.max_bytes is not None,
                bool(self.allowed_formats),
                self.rgb is not None,
            )
        )
        if not filled:
            raise ValueError("at least one param required")
        for fmt in self.allowed_formats:
            if len(fmt) > 16:
                raise ValueError("format name too long")
        return self


class _RuleBase(WireModel):
    # Same cap as ErrorItem.message: the engine copies the formatted text into one.
    message: Annotated[NonBlankStr, Field(max_length=1000)]
    suggestion: Annotated[NonBlankStr | None, Field(default=None, max_length=1000)] = None
    error_code: ErrorCode
    pack: Annotated[str, Field(pattern=r"^[a-z_]{1,32}$")] = "core"
    policy_ref: PolicyRef | None = None
    error_class: ErrorClass | None = None
    retry_class: RetryClass | None = None

    @field_validator("message")
    @classmethod
    def _placeholders(cls, message: str) -> str:
        formatter = string.Formatter()
        for _literal, field_name, spec, conversion in formatter.parse(message):
            if field_name is None:
                continue
            if field_name not in PLACEHOLDERS:
                raise ValueError(f"unknown placeholder {{{field_name}}}")
            # "{value:>999999999}" would make str.format allocate ~1 GB in the engine.
            if spec or conversion:
                raise ValueError(f"format spec or conversion not allowed in {{{field_name}}}")
        if message.count("{") != message.count("}"):
            raise ValueError("unbalanced braces")
        return message


class MaxLength(_RuleBase):
    type: Literal["max_length"]
    limit: Annotated[int, Field(gt=0)]
    unit: Literal["chars", "bytes", "words"] = "chars"
    error_code: ErrorCode = ErrorCode.FIELD_TOO_LONG


class MinLength(_RuleBase):
    type: Literal["min_length"]
    limit: Annotated[int, Field(gt=0)]
    unit: Literal["chars", "bytes", "words"] = "chars"
    error_code: ErrorCode = ErrorCode.FIELD_TOO_SHORT


class BannedTerms(_RuleBase):
    type: Literal["banned_terms"]
    terms: Annotated[list[NonBlankStr], Field(min_length=1, max_length=500)]
    match: Literal["word", "substring", "regex"] = "word"
    case_sensitive: bool = False
    error_code: ErrorCode = ErrorCode.BANNED_TERM


class RequiredAttribute(_RuleBase):
    type: Literal["required_attribute"]
    attribute: AttrKey
    allowed_values: list[str] | None = None
    error_code: ErrorCode = ErrorCode.MISSING_ATTRIBUTE


class MaxItems(_RuleBase):
    type: Literal["max_items"]
    limit: Annotated[int, Field(gt=0)]
    unique: bool = False
    error_code: ErrorCode = ErrorCode.FIELD_TOO_LONG


class MinItems(_RuleBase):
    type: Literal["min_items"]
    limit: Annotated[int, Field(gt=0)]
    error_code: ErrorCode = ErrorCode.FIELD_TOO_SHORT


class AllowedValues(_RuleBase):
    type: Literal["allowed_values"]
    attribute: AttrKey
    source: Literal["inline", "template"]
    values: list[str] | None = None
    normalize: Literal["casefold_trim"] = "casefold_trim"
    error_code: ErrorCode = ErrorCode.VALUE_NOT_ALLOWED


class ConditionalRequired(_RuleBase):
    type: Literal["conditional_required"]
    attribute: AttrKey
    when: Annotated[list[Condition], Field(min_length=1, max_length=4)]
    error_code: ErrorCode = ErrorCode.MISSING_ATTRIBUTE


class CrossField(_RuleBase):
    type: Literal["cross_field"]
    name: NonBlankStr
    left: str
    op: Literal["lte", "lt", "eq", "ne", "gte", "gt", "contains_value_of", "not_contains_value_of"]
    right: str
    error_code: ErrorCode = ErrorCode.CROSS_FIELD_MISMATCH

    @field_validator("left", "right")
    @classmethod
    def _side_path(cls, value: str) -> str:
        if not is_valid_field_path(value):
            raise ValueError("illegal cross_field path")
        return value


class ClaimRequirement(WireModel):
    """What a claim needs (DATABASE.md claim_requires_attribute row): the attribute must be
    present and, when ``any_of`` is set, hold one of those values (``organic`` → certifications
    any_of ``gots``)."""

    attribute: AttrKey
    any_of: Annotated[list[RuleTerm], Field(min_length=1, max_length=50)] | None = None


class ClaimRequiresAttribute(_RuleBase):
    type: Literal["claim_requires_attribute"]
    claim: NonBlankStr
    terms: Annotated[list[NonBlankStr], Field(min_length=1, max_length=50)]
    match: Literal["word", "substring"] = "word"
    requires: ClaimRequirement
    error_code: ErrorCode = ErrorCode.UNSUPPORTED_CLAIM


class ImageRequirement(_RuleBase):
    type: Literal["image_requirement"]
    slot: Literal["main", "secondary", "lifestyle", "size_chart", "any"]
    check: Literal[
        "min_resolution",
        "recommended_resolution",
        "aspect_ratio",
        "background",
        "fill_ratio",
        "text_overlay",
        "watermark",
        "logo",
        "format",
        "max_bytes",
    ]
    params: ImageRequirementParams
    error_code: ErrorCode = ErrorCode.IMAGE_NON_COMPLIANT
    issue_code: ErrorCode = ErrorCode.IMAGE_NON_COMPLIANT


class Pattern(_RuleBase):
    type: Literal["pattern"]
    name: NonBlankStr
    regex: Annotated[str, Field(max_length=256)]
    must: Literal["match", "not_match"]
    flags: list[Literal["i"]] = Field(default_factory=list)
    error_code: ErrorCode = ErrorCode.PATTERN_MISMATCH

    @field_validator("regex")
    @classmethod
    def _regex_safe(cls, regex: str) -> str:
        return check_rule_regex(regex)


FactAttribute = Literal["*critical"] | AttrKey
"""A fact key, or ``*critical`` for every critical fact (DATABASE.md fact_source row)."""


class FactSource(_RuleBase):
    type: Literal["fact_source"]
    attribute: FactAttribute
    forbidden_sources: list[str]
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    require_confirmation_if_agent_captured: bool = False
    error_code: ErrorCode = ErrorCode.FACT_SOURCE_FORBIDDEN


class FactBackedAttribute(_RuleBase):
    type: Literal["fact_backed_attribute"]
    attribute: FactAttribute
    compare: Literal["normalized"] = "normalized"
    error_code: ErrorCode = ErrorCode.FACT_MISMATCH


RuleDef = Annotated[
    MaxLength
    | MinLength
    | BannedTerms
    | RequiredAttribute
    | MaxItems
    | MinItems
    | AllowedValues
    | ConditionalRequired
    | CrossField
    | ClaimRequiresAttribute
    | ImageRequirement
    | Pattern
    | FactSource
    | FactBackedAttribute,
    Field(discriminator="type"),
]


class Rule(WireModel):
    id: UUID
    tenant_id: UUID | None = None
    marketplace: Marketplace
    category: Slug | None = None
    field: str
    severity: Severity = Severity.ERROR
    version: Annotated[int, Field(ge=1)] = 1
    enabled: bool = True
    rule_def: RuleDef

    @model_validator(mode="after")
    def _field_and_def(self) -> Rule:
        if not is_valid_rule_field(self.field):
            raise ValueError("illegal rule field")
        is_image = isinstance(self.rule_def, ImageRequirement)
        if self.field == "images" and not is_image:
            raise ValueError("images field requires image_requirement")
        if is_image and self.field != "images":
            raise ValueError("image_requirement requires images field")
        return self


class RuleSet(WireModel):
    """Carried on GenerationContext.marketplace_rules; the backend resolves it before enqueue.
    PipelineDeps has NO load_rules port — the AI package fetches nothing. Rows group by
    (tenant_id, field, rule_type, subject, pack); a category rule shadows the category=NULL
    rule and the highest enabled version wins. Tenant rules never shadow platform rules: both
    run, so brand rules only tighten."""

    marketplace: Marketplace
    category: Slug
    rules: Annotated[list[Rule], Field(max_length=MAX_RULESET_RULES)] = Field(default_factory=list)
    rules_version: Annotated[int, Field(ge=0)] = 0
    loaded_at: AwareDatetime

    @field_validator("rules")
    @classmethod
    def _count(cls, rules: list[Rule]) -> list[Rule]:
        if len(rules) > MAX_RULESET_RULES:
            raise ValueError("too many rules")
        return rules
