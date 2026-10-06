"""S14a T6: ``banned_terms`` (AC 2; D10a; KNOWN-ISSUES `match="word"`)."""

from __future__ import annotations

from typing import Any

from app.services.rules_engine import evaluate
from tests.rules_engine_builders import TENANT_ID, listing, rule, ruleset


def _banned(terms: list[str], field: str = "content.title", **kw: Any) -> Any:
    over = {k: kw.pop(k) for k in ("tenant_id", "severity") if k in kw}
    return rule(
        {"type": "banned_terms", "terms": terms, "message": "Banned: {term}", **kw},
        field=field,
        **over,
    )


def _errors(item: Any, *rules: Any) -> list[Any]:
    return list(evaluate(item, ruleset(*rules)).errors)


def test_banned_terms_hash_one_word() -> None:
    errs = _errors(listing(content={"title": "Rank #1 kurta set"}), _banned(["#1"]))
    assert [(e.field, e.error_code.value, e.message) for e in errs] == [
        ("content.title", "BANNED_TERM", "Banned: #1")
    ]
    assert errs[0].retryable is True
    assert errs[0].value_submitted == "Rank #1 kurta set"


def test_banned_terms_not_in_secure() -> None:
    assert _errors(listing(content={"title": "Secure fit kurta"}), _banned(["cure"])) == []


def test_banned_terms_case_insensitive() -> None:
    assert len(_errors(listing(content={"title": "BEST kurta"}), _banned(["best"]))) == 1


def test_banned_terms_fullwidth_nfkc() -> None:
    fullwidth = "".join(chr(0xFEE0 + ord(c)) for c in "BEST")
    assert len(_errors(listing(content={"title": f"{fullwidth} kurta"}), _banned(["best"]))) == 1


def test_banned_terms_whitespace_collapsed() -> None:
    item = listing(content={"description": "Order now for free\n  shipping today"})
    assert len(_errors(item, _banned(["free shipping"], "content.description"))) == 1


def test_banned_terms_star_concrete_paths() -> None:
    item = listing(
        content={
            "title": "Kurta",
            "bullets": ["ok", "ok", "doctor recommended fabric"],
            "description": "This kurta cures stress",
        },
        keywords={"search_terms": ["cure kurta"], "hidden_terms": ["fine", "number 1"]},
    )
    errs = _errors(item, _banned(["cure", "cures", "doctor recommended", "number 1"], "*"))
    assert [(e.field, e.message) for e in errs] == [
        ("content.bullets[2]", "Banned: doctor recommended"),
        ("content.description", "Banned: cures"),
        ("keywords.search_terms[0]", "Banned: cure"),
        ("keywords.hidden_terms[1]", "Banned: number 1"),
    ]


def test_banned_terms_star_skips_attributes() -> None:
    item = listing(attributes={"color": "best", "fabric_type": "cotton"})
    assert _errors(item, _banned(["best"], "*")) == []
    assert len(_errors(item, _banned(["best"], "attributes.color"))) == 1


def test_banned_terms_list_attribute_one_item_per_path() -> None:
    item = listing(attributes={"tags": ["best", "cure", "fine"]})
    errs = _errors(item, _banned(["cure", "best"], "attributes.tags"))
    assert [(e.field, e.message, e.value_submitted) for e in errs] == [
        ("attributes.tags", "Banned: cure, best", "best cure")
    ]


def test_banned_terms_substring_pass_fail() -> None:
    item = listing(content={"title": "Secure fit"})
    assert len(_errors(item, _banned(["cure"], match="substring"))) == 1
    assert _errors(item, _banned(["xyz"], match="substring")) == []


def test_banned_terms_regex_pass_fail() -> None:
    item = listing(content={"title": "Kurta 100% cotton"})
    assert len(_errors(item, _banned([r"\d+%"], match="regex"))) == 1
    assert _errors(item, _banned([r"^\d+$"], match="regex")) == []


def test_banned_terms_case_sensitive_pass_fail() -> None:
    item = listing(content={"title": "Best kurta"})
    assert _errors(item, _banned(["best"], case_sensitive=True)) == []
    assert len(_errors(item, _banned(["Best"], case_sensitive=True))) == 1


def test_banned_terms_multiple_terms_one_item() -> None:
    item = listing(content={"title": "Best guaranteed kurta"})
    errs = _errors(item, _banned(["guaranteed", "best", "none"]))
    assert [e.message for e in errs] == ["Banned: guaranteed, best"]


def test_banned_terms_tenant_and_platform_both_fire() -> None:
    item = listing(content={"title": "Best kurta"})
    platform = _banned(["best"])
    tenant = _banned(["best"], tenant_id=TENANT_ID)
    errs = _errors(item, tenant, platform)
    assert [e.rule_id for e in errs] == [platform.id, tenant.id]


def test_banned_terms_warning_does_not_fail() -> None:
    rep = evaluate(
        listing(content={"title": "Best kurta"}), ruleset(_banned(["best"], severity="warning"))
    )
    assert rep.status == "passed"
    assert rep.errors[0].severity.value == "warning"
