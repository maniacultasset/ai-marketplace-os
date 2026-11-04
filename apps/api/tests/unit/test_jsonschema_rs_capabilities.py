"""DW2 T0: pins the `jsonschema-rs` behaviour the PTD schema check relies on.

A library upgrade that changes any of these fails here first, before the schema check
(`app.adapters.amazon.schema_check`) silently changes what it reports.
"""

from __future__ import annotations

import socket
import time
from typing import Any, ClassVar

import jsonschema_rs
import pytest


def _kind(error: jsonschema_rs.ValidationError) -> str:
    return type(error.kind).__name__


def test_draft_2019_09_resolves_local_defs_and_reports_instance_path() -> None:
    schema = {
        "$defs": {"short": {"type": "string", "maxLength": 2}},
        "type": "object",
        "properties": {"x": {"$ref": "#/$defs/short"}},
    }
    validator = jsonschema_rs.Draft201909Validator(schema, offline=True)
    errors = list(validator.iter_errors({"x": "abc"}))
    assert [(list(e.instance_path), _kind(e)) for e in errors] == [(["x"], "MaxLength")]
    assert validator.is_valid({"x": "ab"})


class _Flag:
    calls: ClassVar[list[tuple[Any, Any]]] = []

    def __init__(self, parent_schema: dict[str, Any], value: Any, schema_path: list[Any]) -> None:
        self.value = value
        self.sibling = parent_schema.get("selectors")

    def validate(self, instance: Any) -> None:
        type(self).calls.append((self.value, self.sibling))
        if instance == "bad":
            raise ValueError("flagged")


def test_custom_keyword_is_called_with_its_sibling_and_reports_kind_custom() -> None:
    _Flag.calls = []
    schema = {"properties": {"a": {"flag": 3, "selectors": ["marketplace_id"]}}}
    validator = jsonschema_rs.Draft201909Validator(schema, keywords={"flag": _Flag}, offline=True)
    errors = list(validator.iter_errors({"a": "bad"}))
    assert len(errors) == 1
    assert _kind(errors[0]) == "Custom"
    assert errors[0].kind.as_dict()["keyword"] == "flag"
    assert list(errors[0].instance_path) == ["a"]
    assert _Flag.calls == [(3, ["marketplace_id"])]


def test_external_ref_offline_fails_at_compile_time_without_a_socket() -> None:
    def _no_socket(*_a: Any, **_k: Any) -> Any:  # pragma: no cover - must never run
        raise AssertionError("socket opened")

    original = socket.socket.connect
    socket.socket.connect = _no_socket  # type: ignore[method-assign]
    try:
        with pytest.raises(jsonschema_rs.ValidationError) as caught:
            jsonschema_rs.Draft201909Validator(
                {"$ref": "https://example.invalid/x.json#/a"}, offline=True
            )
    finally:
        socket.socket.connect = original  # type: ignore[method-assign]
    assert "Retrieval is disabled" in str(caught.value)


def test_unknown_meta_schema_uri_compiles_when_the_draft_is_forced() -> None:
    # Recorded behaviour: a forced draft ignores an unknown `$schema`; the check still strips it
    # (T5) so a later library that resolves `$schema` cannot reach the network.
    schema = {
        "$schema": "https://schemas.amazon.com/selling-partners/definitions/product-types/"
        "meta-schema/v1",
        "type": "string",
    }
    validator = jsonschema_rs.Draft201909Validator(schema, offline=True)
    assert not validator.is_valid(1)


def test_mask_keeps_the_instance_value_out_of_messages() -> None:
    validator = jsonschema_rs.Draft201909Validator(
        {"enum": ["a"], "maxLength": 1}, mask="<value>", offline=True
    )
    messages = [e.message for e in validator.iter_errors("कुर्ता")]
    assert messages
    assert all("कुर्ता" not in m for m in messages)


def test_backtrack_limit_reports_an_error_instead_of_hanging() -> None:
    validator = jsonschema_rs.Draft201909Validator(
        {"type": "string", "pattern": r"^(a|a)+\1$"},
        pattern_options=jsonschema_rs.FancyRegexOptions(backtrack_limit=10_000),
        mask="<value>",
        offline=True,
    )
    started = time.monotonic()
    errors = list(validator.iter_errors("a" * 5000 + "c"))
    assert time.monotonic() - started < 1.0
    assert [_kind(e) for e in errors] == ["BacktrackLimitExceeded"]


def test_bad_regex_fails_at_compile_time() -> None:
    with pytest.raises(jsonschema_rs.ValidationError):
        jsonschema_rs.Draft201909Validator({"pattern": "(unclosed"}, offline=True)


def test_formats_are_not_checked_when_disabled() -> None:
    validator = jsonschema_rs.Draft201909Validator(
        {"type": "string", "format": "uri"}, validate_formats=False, offline=True
    )
    assert validator.is_valid("::not a uri::")
