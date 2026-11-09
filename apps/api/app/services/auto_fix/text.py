"""Templated trail and question text for the fix pass (SI4 A4, A11; T54: no LLM text in v1).

No template ever formats a listing value: the trail names the slot, the fix and the sizes, so the
audit `reason` and the issue's `action_taken` never copy seller or model text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import FixAction
from mkos_schemas.jobs import CategoryTemplate

ACTION_MIN: Final = 5
ACTION_MAX: Final = 1000

FIELD_LABELS: Final[Mapping[str, str]] = {
    "content.title": "title",
    "content.description": "description",
    "content.bullets": "bullet point",
    "keywords.search_terms": "search terms",
    "keywords.hidden_terms": "hidden search terms",
}
_UNITS: Final[Mapping[str, tuple[str, str]]] = {
    "chars": ("character", "characters"),
    "bytes": ("byte", "bytes"),
    "words": ("word", "words"),
    "values": ("value", "values"),
    "terms": ("term", "terms"),
}


def field_label(field: str) -> str:
    """A plain name for a field path: `content.bullets[2]` → "bullet point 3",
    `attributes.fit_type` → "fit type"."""
    if field.startswith("content.bullets[") and field.endswith("]"):
        index = field[len("content.bullets[") : -1]
        return f"bullet point {int(index) + 1}" if index.isdigit() else "bullet point"
    if field in FIELD_LABELS:
        return FIELD_LABELS[field]
    if field.startswith("attributes."):
        return field.removeprefix("attributes.").replace("_", " ")
    return field


def _unit(unit: str, count: int) -> str:
    singular, plural = _UNITS.get(unit, (unit, unit))
    return singular if count == 1 else plural


def _cap(text: str) -> str:
    return text[:ACTION_MAX]


def action_taken(
    fix_action: FixAction,
    field: str,
    *,
    before_size: int,
    after_size: int,
    unit: str,
    limit: int | None,
    duplicates: int = 0,
    trimmed: bool = True,
) -> str:
    """One sentence per fix action and slot family; 5..1000 characters; never a value."""
    label = field_label(field)
    if fix_action is FixAction.REMAP_VALUE:
        return _cap(
            f"Matched the {label} value to the spelling the marketplace accepts "
            f"({after_size} {_unit('values', after_size)} changed)."
        )
    if fix_action is not FixAction.REFORMAT:
        return _cap(f"Applied the {fix_action.value} fix to the {label}.")
    sizes = f"from {before_size} to {after_size} {_unit(unit, after_size)}"
    bound = "" if limit is None else f" to fit the {limit}-{_unit(unit, 1)} limit"
    if field.startswith("keywords."):
        parts: list[str] = []
        if duplicates:
            parts.append(f"removed {duplicates} repeated {_unit('terms', duplicates)}")
        if trimmed:
            parts.append("dropped whole terms from the end")
        done = " and ".join(parts) or "cleaned up spacing"
        return _cap(f"In the {label}, {done}, {sizes}{bound}.")
    if not trimmed:
        return _cap(f"Cleaned up spacing in the {label}, {sizes}{bound}.")
    return _cap(f"Shortened the {label} at a word boundary {sizes}{bound}.")


# ---- A11: the one clear question (spec L1029-1037; DATABASE.md:1407 AnswerSpec) ----

QUESTION_MIN: Final = 5
QUESTION_MAX: Final = 500  # ck_human_tasks_question / ck_human_tasks_reason
LABEL_MAX: Final = 64
AFFECTED_MAX: Final = 300
TEXT_ANSWER_MAX: Final = 256
ANSWER_KINDS: Final = frozenset(
    {
        "enum",
        "multi_enum",
        "text",
        "number",
        "money_inr",
        "percent_composition",
        "boolean",
        "file",
        "acknowledge",
        "reconnect",
    }
)
_EXAMPLES: Final[Mapping[str, str]] = {
    "composition": " For example: 95% cotton, 5% elastane.",
    "money": " Give the amount in rupees.",
    "measure": " Give a number with its unit.",
    "number": " Give a number.",
    "integer": " Give a whole number.",
    "boolean": " Answer yes or no.",
    "enum": " Pick one of the listed values.",
    "enum_list": " Pick every value that applies.",
}
_MARKETPLACES: Final[Mapping[str, str]] = {"amazon": "Amazon", "flipkart": "Flipkart"}

# code -> (question, reason); `{label}` is the only placeholder, and it is never a value
_QUESTIONS: Final[Mapping[ErrorCode, tuple[str, str]]] = {
    ErrorCode.MISSING_ATTRIBUTE: (
        "Add the {label} on the product.",
        "The marketplace needs the {label} to list this product, and the system never guesses "
        "a product fact.",
    ),
    ErrorCode.CRITICAL_FACT_MISSING: (
        "Add the {label} on the product.",
        "The {label} is a fact the listing must state exactly; it can only come from you.",
    ),
    ErrorCode.REQUIRED_FIELD_MISSING: (
        "Add the {label} on the product.",
        "The marketplace refuses the listing without the {label}.",
    ),
    ErrorCode.PRICE_ABOVE_MRP: (
        "Check the {label} and the selling price on the product.",
        "The selling price is above the MRP; a marketplace refuses that.",
    ),
    ErrorCode.FACT_NEEDS_CONFIRMATION: (
        "Confirm the {label} on the product.",
        "The {label} was not given by you, so it needs your confirmation before it is used.",
    ),
    ErrorCode.FACT_MISMATCH: (
        "Check the {label} on the product.",
        "The listing and the product facts disagree on the {label}; only you can say which is "
        "right.",
    ),
    ErrorCode.COMPLIANCE_DECLARATION_MISSING: (
        "Add the {label} declaration on the product.",
        "The {label} is a compliance declaration the marketplace requires.",
    ),
    ErrorCode.IDENTIFIER_MISSING: (
        # QA 2026-11-09: name what an identifier is, and the exemption a brand may hold
        "Add the {label} on the product. Use its EAN, UPC or GTIN, or a GTIN exemption.",
        "The marketplace needs the {label} to identify this product, or a GTIN exemption if it "
        "approved one for your brand.",
    ),
    ErrorCode.VALUE_NOT_ALLOWED: (
        "Set the {label} on the product to one of the values the marketplace accepts.",
        "The {label} value is not one the marketplace accepts, and it does not match an "
        "accepted value closely enough to change it automatically.",
    ),
}
_DEFAULT_QUESTION: Final = (
    "Check the {label} on the product.",
    "The marketplace cannot accept the listing until the {label} is fixed.",
)


# QA 2026-11-09: the seller's word for a key whose name is internal
KEY_WORDS: Final[Mapping[str, str]] = {"identifier": "barcode"}


def _label(key: str, label: str) -> str:
    shown = " ".join(label.split())[:LABEL_MAX].strip()
    return shown or KEY_WORDS.get(key) or key.replace("_", " ")[:LABEL_MAX]


def _fit(text: str) -> str:
    return text[:QUESTION_MAX]


def question_for(
    code: ErrorCode, key: str, label: str, *, value_type: str | None = None
) -> tuple[str, str]:
    """The question and the reason for one missing or refused fact, both 5..500 characters.
    They say why the value is needed, what to give and where to set it; never a value."""
    shown = _label(key, label)
    question, reason = _QUESTIONS.get(code, _DEFAULT_QUESTION)
    hint = _EXAMPLES.get(value_type or "", "")
    return _fit(question.format(label=shown) + hint), _fit(reason.format(label=shown))


def _fold(value: str) -> str:
    return value.strip().casefold()


def _unique(values: Sequence[str], exclude: frozenset[str]) -> list[str]:
    """`values` once each by the Rules fold (the first spelling kept), less `exclude`."""
    out: dict[str, str] = {}
    for v in values:
        folded = _fold(v)
        if folded not in exclude:
            out.setdefault(folded, v)
    return list(out.values())


def answer_choices(
    vocabulary: Sequence[str] | None,
    template: CategoryTemplate | None,
    key: str,
    *,
    exclude: Sequence[str] = (),
) -> list[str] | None:
    """SI9 CP-D23: the choices a question for `key` offers. With an enum spec on the listing's
    template: the dictionary vocabulary kept to the template's accepted spellings (matched with
    the Rules fold, `strip().casefold()`), else the template's allowed values; otherwise the
    vocabulary. QA 2026-11-09: each choice once by the fold (a `value_map` key is an alias of
    an allowed value, never a choice of its own when allowed values exist), and never a value
    in `exclude` (the value the marketplace just refused)."""
    refused = frozenset(_fold(v) for v in exclude)
    spec = None if template is None else template.specs.get(key)
    spellings = (
        None
        if spec is None or (spec.allowed_values is None and not spec.value_map)
        else [*(spec.allowed_values or []), *sorted(spec.value_map)]
    )  # the Rules check's accepted list (`rules_engine.template.template_allowed`)
    if spellings is None:
        return _unique(vocabulary, refused) or None if vocabulary else None
    accepted = {_fold(s) for s in spellings}
    kept = _unique([v for v in (vocabulary or ()) if _fold(v) in accepted], refused)
    if kept:
        return kept
    base = list(spec.allowed_values or []) if spec is not None and spec.allowed_values else []
    if not base and spec is not None:
        base = sorted(spec.value_map)
    return _unique(base, refused) or None


def expected_answer(
    value_type: str, *, vocabulary: Sequence[str] | None, unit: str | None
) -> dict[str, Any]:
    """The AnswerSpec for a dictionary value type (DATABASE.md:1407)."""
    if value_type == "enum" and vocabulary:
        return {"kind": "enum", "allowed_values": list(vocabulary)}
    if value_type == "enum_list" and vocabulary:
        return {"kind": "multi_enum", "allowed_values": list(vocabulary)}
    if value_type in ("number", "integer", "measure"):
        return {"kind": "number"} if unit is None else {"kind": "number", "unit": unit}
    if value_type == "money":
        return {"kind": "money_inr"}
    if value_type == "composition":
        return {"kind": "percent_composition"}
    if value_type == "boolean":
        return {"kind": "boolean"}
    return {"kind": "text", "max_length": TEXT_ANSWER_MAX}


# Prod demo 9 Nov: the reason named raw check codes ("BACKGROUND OFF, RESOLUTION LOW, WRONG
# ASPECT"). Each code (an `ImageIssueCode`, the row's `marketplace_code`) and each check name (the
# Pillow and vision `CheckName`) in seller words; anything else reads as the generic last entry.
IMAGE_PROBLEMS: Final[Mapping[str, str]] = {
    "BACKGROUND_OFF": "the background is not plain white",
    "RESOLUTION_LOW": "the photo is smaller than the marketplace asks for",
    "WRONG_ASPECT": "the photo is not the shape the marketplace asks for",
    "TEXT_DETECTED": "there is text on the photo",
    "WATERMARK": "the photo has a watermark",
    "FILL_RATIO_LOW": "the product fills too little of the photo",
    "FORMAT_UNSUPPORTED": "the file type is not accepted (use JPEG or PNG)",
    "FILE_TOO_LARGE": "the file is too large",
    "CORRUPT": "the file could not be opened",
    "LOGO_PROHIBITED": "the photo shows a logo the marketplace does not allow",
    "background": "the background is not plain white",
    "resolution": "the photo is smaller than the marketplace asks for",
    "aspect_ratio": "the photo is not the shape the marketplace asks for",
    "text_overlay": "there is text on the photo",
    "watermark": "the photo has a watermark",
    "fill_ratio": "the product fills too little of the photo",
    "format": "the file type is not accepted (use JPEG or PNG)",
    "file_size": "the file is too large",
    "corruption": "the file could not be opened",
    "logo": "the photo shows a logo the marketplace does not allow",
    "exif_orientation": "the photo is turned on its side",
}
IMAGE_PROBLEM_DEFAULT: Final = "it does not meet the marketplace photo rules"


def image_problem(check: str) -> str:
    """One failed image check or code in seller words."""
    return IMAGE_PROBLEMS.get(check, IMAGE_PROBLEM_DEFAULT)


def image_question(slot: str, checks: Sequence[str]) -> tuple[str, str]:
    """The question and the reason for an image that failed its marketplace checks: each problem
    once, in plain words (never the check code)."""
    problems = list(dict.fromkeys(image_problem(c) for c in checks)) or [IMAGE_PROBLEM_DEFAULT]
    question = f"Upload a new {slot.replace('_', ' ')} image for this product."
    reason = "The current photo can't be used: " + "; ".join(problems) + "."
    if len(reason) > QUESTION_MAX:
        reason = reason[: QUESTION_MAX - 4].rstrip(" ,;") + " ..."
    return _fit(question), _fit(reason)


def affected(listings: Sequence[tuple[str, str]]) -> str:
    """The "which products or variants it affects" clause, from (SKU, marketplace) pairs."""
    if not listings:
        return ""
    parts = [f"{sku} on {_MARKETPLACES.get(m, m)}" for sku, m in listings]
    text = "It affects " + ", ".join(parts) + "."
    if len(text) > AFFECTED_MAX:
        text = text[: AFFECTED_MAX - 4].rstrip(" ,") + " ..."
    return text


# ---- SI11 T18 and the staff lane: the next step of a listing Kuinn cannot fix itself ----

LINK_MAX: Final = 2048
_STAFF_STEPS: Final[Mapping[str, tuple[str, str]]] = {
    "repair_rejected": (
        "Review a marketplace error Kuinn's automatic fix did not clear.",
        "The marketplace refused the value Kuinn's adapter fix sent, so Kuinn stopped guessing "
        "(one automatic try per attribute); a person decides the next value.",
    ),
    "recurrence": (
        "Review a marketplace error that came back after Kuinn fixed it.",
        "The marketplace raised an error Kuinn had already fixed once, so Kuinn stopped "
        "retrying it; a person decides the next step.",
    ),
    "unanswerable_fact": (
        "Review a marketplace error on a product fact the seller cannot set.",
        "The marketplace refused a product fact the seller's inbox cannot answer; a person "
        "checks the mapping.",
    ),
    "content_review": (
        "Review a listing text or keyword the marketplace refused.",
        "The marketplace refused this listing's text or keywords; Kuinn does not change content "
        "on its own yet, so a person edits it and another person approves the new version.",
    ),
    "mapping": (
        "Review a marketplace error Kuinn could not trace to a product field.",
        "The marketplace refused this listing without naming a field the seller can answer, so "
        "a person checks how Kuinn maps it.",
    ),
    "ops": (
        "Review a marketplace error Kuinn routes to staff.",
        "The marketplace refused this listing with an error only staff can act on.",
    ),
}
STAFF_DEFAULT: Final = "ops"


def staff_step(action: str) -> tuple[str, str]:
    """The question and the reason of the staff review task for one planner action."""
    question, reason = _STAFF_STEPS.get(action, _STAFF_STEPS[STAFF_DEFAULT])
    return _fit(question), _fit(reason)


def seller_central_step(
    code: str | None, category: str | None, link: str | None = None
) -> tuple[str, str]:
    """T18 (T54: templated): the clear next step of a marketplace-only error, 5..500 characters.
    A link is added only when the issue carries one that is `https://` and at most 2048 long."""
    where = f" for the {category.replace('_', ' ')} category" if category else ""
    question = f"Request approval in Seller Central{where}, then resume publishing."
    shown = f" (code {code})" if code else ""
    reason = (
        f"Only the marketplace can approve this brand or category{shown}; Kuinn does not retry "
        "until a person closes this task."
    )
    if link and link.startswith("https://") and len(link) <= LINK_MAX:
        step = f" See {link}"
        if len(question) + len(step) <= QUESTION_MAX:
            question += step
    return _fit(question), _fit(reason)


REWRITE_MAX: Final = 500


def rewrite_feedback(marketplace: str, field: str, plain: str) -> str:
    """SI11 T16: the one feedback line an AI single-field rewrite gets per refusal (at most 500
    characters): the marketplace, the field's plain name and the mapped plain message, never the
    raw marketplace text beyond it."""
    who = (
        "Amazon"
        if marketplace == "amazon"
        else "Flipkart" if marketplace == "flipkart" else "The marketplace"
    )
    message = " ".join((plain or "").split())[:300] or "the value was not accepted"
    return f"{who} refused {field_label(field)}: {message} Rewrite only this field."[:REWRITE_MAX]
