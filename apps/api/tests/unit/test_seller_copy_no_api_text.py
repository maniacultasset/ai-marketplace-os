"""Client feedback pass 2, B13: a seller never reads an API path or an HTTP verb.

The mapping page showed "set the product's gender (PATCH /api/v1/products/{product_id})". Every
text a seller can read says the next step in plain words instead ("Add the gender on the product
page"), and the web links the field to the product page. This scans every source of that text:

- the prose string literals (docstrings left out) of the adapters, the Rules Engine, the fix
  policy and its questions, the issue, task and review services: the mapping messages, the
  remedies, the findings and the questions;
- the error-map seed rows (plain text and seller action) of every marketplace;
- the platform rule seed (each rule's message and suggestion).
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

APP = Path(__file__).resolve().parents[2] / "app"
REPO = Path(__file__).resolve().parents[4]

# An API path, or an HTTP verb followed by a path ("PATCH /products/..."), anywhere in the text.
API_TEXT = re.compile(r"/api/|\b(?:GET|POST|PUT|PATCH|DELETE)\s+/")

SELLER_SOURCES = (
    "adapters",
    "services/rules_engine",
    "services/auto_fix",
    "services/resolution",
    "services/issues.py",
    "services/issue_questions.py",
    "services/tasks.py",
    "services/home.py",
    "services/listing_validate.py",
    "services/listing_display.py",
    "services/field_requirements.py",
    "services/photo_facts.py",
    "services/photo_match.py",
    "services/image_checks.py",
    "services/image_gate.py",
    "services/category_gate.py",
    "services/quality.py",
    "services/publish.py",
    "services/adapter_check.py",
)


def _files() -> Iterator[Path]:
    for name in SELLER_SOURCES:
        path = APP / name
        if path.is_dir():
            yield from sorted(path.rglob("*.py"))
        else:
            assert path.exists(), name  # a moved module must stay in the scan
            yield path


def _docstrings(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                out.add(id(first.value))
    return out


def _prose_literals(path: Path) -> Iterator[tuple[int, str]]:
    """Each string literal with a space in it (text, not a bare route or key), docstrings out."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = _docstrings(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docs
            and " " in node.value.strip()
        ):
            yield node.lineno, node.value


def test_the_scan_reads_the_seller_sources() -> None:
    files = list(_files())
    assert any(f.name == "mapping.py" and f.parent.name == "amazon" for f in files)
    assert len(files) > 20


@pytest.mark.parametrize(
    "text", ["(PATCH /api/v1/products/{product_id})", "POST /tasks/x", "/api/v1"]
)
def test_the_pattern_catches_api_text(text: str) -> None:
    assert API_TEXT.search(text)


@pytest.mark.parametrize(
    "text", ["Add the gender on the product page.", "Sent 3/4 sleeve", "POSTER"]
)
def test_the_pattern_leaves_plain_words(text: str) -> None:
    assert not API_TEXT.search(text)


def test_no_api_text_in_seller_strings() -> None:
    found = [
        f"{path.relative_to(APP)}:{line}: {text[:80]!r}"
        for path in _files()
        for line, text in _prose_literals(path)
        if API_TEXT.search(text)
    ]
    assert not found, "seller-facing text names an API path or verb:\n" + "\n".join(found)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


SEED = APP / "db" / "seed_data"


@pytest.mark.parametrize(
    "name", ["error_map.amazon.json", "error_map.flipkart.json", "error_map.fake.json"]
)
def test_no_api_text_in_error_map_rows(name: str) -> None:
    rows = json.loads((SEED / name).read_text(encoding="utf-8"))
    assert rows
    bad = [
        (row.get("raw_value"), text)
        for row in rows
        for key in ("plain_text", "seller_action")
        for text in _strings(row.get(key))
        if API_TEXT.search(text)
    ]
    assert not bad


def test_no_api_text_in_platform_rule_messages() -> None:
    sets = json.loads((REPO / "packages/schemas/fixtures/rules.seed.json").read_text("utf-8"))
    texts = [
        text
        for ruleset in sets
        for rule in ruleset["rules"]
        for key in ("message", "suggestion")
        for text in _strings(rule["rule_def"].get(key))
    ]
    assert texts
    assert not [t for t in texts if API_TEXT.search(t)]
