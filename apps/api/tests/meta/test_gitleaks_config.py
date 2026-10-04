"""AC15: .gitleaks.toml extends the default rules and allowlists exact dev values only."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

DEV_VALUES = {
    "dev_only_change_me_at_least_32_characters_long",  # ENV.md JWT_SECRET row
    "ci_only_not_a_real_secret_at_least_32_chars",  # CANON-ADDENDUM.md:264
    "local_only_schema_export_secret_32_chars",  # CANON-ADDENDUM.md:293
    "dev_only_admin_session_secret_at_least_32_chars",  # ENV.md ADMIN_SESSION_SECRET row
    "bG9jYWxfZGV2X29ubHlfMzJfYnl0ZV9rZXlfMDAwMDA=",  # ENV.md KMS_LOCAL_DEV_KEY row
    "local_only_minio_root_user",  # ENV.md S3_ACCESS_KEY / S3_SECRET_KEY rows
    "local_only_minio_root_password_32_chars",
}
FALSE_POSITIVE_VALUES = {"parent/child"}


@pytest.fixture
def config(repo_root: Path) -> dict[str, Any]:
    with (repo_root / ".gitleaks.toml").open("rb") as fh:
        return tomllib.load(fh)


def _allowlists(config: dict[str, Any]) -> list[dict[str, Any]]:
    lists = config.get("allowlists", [])
    return [*lists, config["allowlist"]] if "allowlist" in config else list(lists)


def _values(config: dict[str, Any]) -> set[str]:
    values = set()
    for allowlist in _allowlists(config):
        for pattern in allowlist.get("regexes", []):
            match = re.fullmatch(r"\^(.+)\$", pattern)
            assert match, f"not anchored: {pattern!r}"
            values.add(re.sub(r"\\(.)", r"\1", match.group(1)))
    return values


def test_extends_default_rules(config: dict[str, Any]) -> None:
    assert config["extend"] == {"useDefault": True}
    assert "rules" not in config


def test_allowlist_exact_anchored_values_only(config: dict[str, Any]) -> None:
    allowlists = _allowlists(config)
    assert allowlists
    for allowlist in allowlists:
        assert set(allowlist) <= {"description", "regexes", "regexTarget"}, allowlist.keys()
        assert allowlist.get("regexTarget", "secret") == "secret"
        for pattern in allowlist["regexes"]:
            value = re.fullmatch(r"\^(.+)\$", pattern)
            assert value is not None
            assert pattern == "^" + re.escape(re.sub(r"\\(.)", r"\1", value.group(1))) + "$"


def test_allowlisted_values_are_repo_dev_values(config: dict[str, Any], repo_root: Path) -> None:
    values = _values(config)
    assert values == DEV_VALUES | FALSE_POSITIVE_VALUES
    committed = (repo_root / ".env.example").read_text(encoding="utf-8") + (
        repo_root / "apps/api/app/core/config.py"
    ).read_text(encoding="utf-8")
    assert not [value for value in DEV_VALUES if value not in committed]
    assert "parent/child" not in committed


def test_false_positive_values_are_exact_parent_child() -> None:
    assert {"parent/child"} == FALSE_POSITIVE_VALUES


def test_parent_child_allowlist_rejects_prefix_and_suffix(config: dict[str, Any]) -> None:
    patterns = [
        pattern
        for allowlist in _allowlists(config)
        for pattern in allowlist.get("regexes", [])
        if "parent/child" in pattern
    ]
    assert patterns == [r"^parent/child$"]
    compiled = re.compile(patterns[0])
    assert compiled.fullmatch("parent/child")
    assert compiled.fullmatch("xparent/child") is None
    assert compiled.fullmatch("parent/child/extra") is None
    assert compiled.fullmatch("parent/child ") is None
    assert compiled.fullmatch(" parent/child") is None


def test_gitleaksignore_pins_only_the_known_history_false_positives(repo_root: Path) -> None:
    entries = [
        line
        for line in (repo_root / ".gitleaksignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert entries == [
        "be02277054e319009eee09390040f8ca0da8d940:docs/trace/c02.json:generic-api-key:2253",
        "be02277054e319009eee09390040f8ca0da8d940:docs/review/REQUIREMENTS-TRACE.md:"
        "generic-api-key:520",
    ]
