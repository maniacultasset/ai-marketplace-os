"""ErrorCode and ImageIssueCode registry tests."""

from __future__ import annotations

import re
from types import MappingProxyType

from mkos_schemas.codes import ERROR_CODE_META, CodeMeta, ErrorCode, ImageIssueCode
from mkos_schemas.enums import RetryClass

from . import canon_sources

_CODE_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]+$")


def _enum_members_from_block(block: str, class_name: str) -> set[str]:
    out: set[str] = set()
    in_class = False
    for line in block.splitlines():
        if f"class {class_name}" in line:
            in_class = True
            continue
        if in_class:
            if line.startswith("class ") or (line.strip() and not line[0].isspace()):
                break
            m = re.match(r'\s+(\w+)\s*=\s*"([^"]+)"', line)
            if m:
                out.add(m.group(1))
            for part in line.split(";"):
                m2 = re.match(r'\s*(\w+)\s*=\s*"([^"]+)"', part.strip())
                if m2:
                    out.add(m2.group(1))
    if not out:
        raise ValueError(f"no members parsed for {class_name}")
    return out


def _assignment_members(block: str) -> set[str]:
    out: set[str] = set()
    for line in block.splitlines():
        m = re.match(r'\s+(\w+)\s*=\s*"([^"]+)"', line)
        if m:
            out.add(m.group(1))
    if not out:
        raise ValueError("no assignment members parsed")
    return out


def _a20_import_codes() -> set[str]:
    block = canon_sources.addendum_block("20")
    members = _assignment_members(block)
    return {c for c in members if c.startswith("HEADER_") or c == "DUPLICATE_SKU_IN_FILE"}


def _a20_all_codes() -> set[str]:
    block = canon_sources.addendum_block("20")
    return _assignment_members(block)


def _draft_error_codes() -> set[str]:
    block = canon_sources.draft_block("codes.py — the single error-code registry")
    return _enum_members_from_block(block, "ErrorCode")


def _database_merged_codes() -> set[str]:
    line = canon_sources.database_line_text("additions, one merged list (Tech Lead)")
    codes: set[str] = set()
    for m in re.finditer(r"`([^`]+)`", line):
        for token in re.findall(r"[A-Z][A-Z0-9_]+", m.group(1)):
            if _CODE_TOKEN.match(token):
                codes.add(token)
    return codes


def _meta_tuple(meta: CodeMeta) -> tuple[str | None, str, str | None]:
    ec = meta.error_class.value if meta.error_class is not None else None
    fa = meta.fix_action.value if meta.fix_action is not None else None
    return (ec, meta.retry_class.value, fa)


def test_codes_import_header_codes_present() -> None:
    import_codes = _a20_import_codes()
    assert import_codes == {
        "HEADER_MISSING_COLUMN",
        "HEADER_UNKNOWN_COLUMN",
        "HEADER_DUPLICATE_COLUMN",
        "DUPLICATE_SKU_IN_FILE",
    }
    for name in import_codes:
        assert getattr(ErrorCode, name) is not None


def test_codes_job_timeout_beside_timeout() -> None:
    a20 = _a20_all_codes()
    assert "JOB_TIMEOUT" in a20
    assert ErrorCode.JOB_TIMEOUT.value == "JOB_TIMEOUT"
    assert ErrorCode.TIMEOUT.value == "TIMEOUT"
    assert len({ErrorCode.JOB_TIMEOUT, ErrorCode.TIMEOUT}) == 2


def test_codes_only_field_too_long() -> None:
    too_long = {m.name for m in ErrorCode if m.name.endswith("_TOO_LONG")}
    assert too_long == {"FIELD_TOO_LONG"}


API_CODES = frozenset(
    {
        "UNAUTHENTICATED",
        "PERMISSION_DENIED",
        "NOT_FOUND",
        "METHOD_NOT_ALLOWED",
        "IDEMPOTENCY_KEY_REUSED",
        "STALE_VERSION",
        "PAYLOAD_TOO_LARGE",
        "NOT_IMPLEMENTED",
        "OAUTH_STATE_INVALID",
        "LAST_OWNER_REQUIRED",
        "MARKETPLACE_ACCOUNT_CONFLICT",
    }
)


def test_codes_match_sources() -> None:
    expected = (
        _draft_error_codes()
        | _a20_all_codes()
        | _database_merged_codes()
        | {"BUDGET_EXCEEDED"}
        | canon_sources.ba_api_codes()
        | DPP_1_CODES
    )
    assert {m.name for m in ErrorCode} == expected
    assert len(ErrorCode) == 78


# 2.0.0 (DPP-1, ADR-0021; Tech Lead OK 2026-10-27): the two step-up and enrolment refusals (403).
DPP_1_CODES = frozenset({"MFA_ENROLLMENT_REQUIRED", "MFA_STEP_UP_REQUIRED"})


def test_mfa_codes_are_listed_with_meta() -> None:
    for name in DPP_1_CODES:
        code = ErrorCode(name)
        assert code.name == name
        assert _meta_tuple(ERROR_CODE_META[code]) == (None, "user_action", None)
    assert ErrorCode.MFA_STEP_UP_REQUIRED.value == "MFA_STEP_UP_REQUIRED"
    assert ErrorCode.MFA_ENROLLMENT_REQUIRED.value == "MFA_ENROLLMENT_REQUIRED"


def test_ba_api_codes_line_names_the_eleven() -> None:
    assert canon_sources.ba_api_codes() == API_CODES


def test_codes_name_equals_value() -> None:
    for m in ErrorCode:
        assert m.name == m.value


def test_codes_maker_checker_present() -> None:
    assert ErrorCode.MAKER_CHECKER_VIOLATION.value == "MAKER_CHECKER_VIOLATION"


def test_codes_budget_exceeded_present() -> None:
    assert ErrorCode.BUDGET_EXCEEDED.value == "BUDGET_EXCEEDED"


def test_stale_version_is_the_only_stale_code() -> None:
    # S2 Decision 5 kept STALE_* out of 1.0.0; S3 D2 adds exactly one, STALE_VERSION (409).
    assert {m.name for m in ErrorCode if "STALE" in m.name} == {"STALE_VERSION"}


def test_image_issue_codes_match_draft() -> None:
    block = canon_sources.draft_block("codes.py — the single error-code registry")
    expected = _enum_members_from_block(block, "ImageIssueCode")
    assert {m.name for m in ImageIssueCode} == expected


def test_t84_vision_failures_use_one_code() -> None:
    assert ErrorCode.IMAGE_NON_COMPLIANT.value == "IMAGE_NON_COMPLIANT"
    overlap = {m.name for m in ImageIssueCode} & {m.name for m in ErrorCode}
    assert overlap == set()


def test_error_code_meta_covers_every_code() -> None:
    assert set(ERROR_CODE_META.keys()) == set(ErrorCode)


def test_error_code_meta_matches_appendix_a() -> None:
    # rev 3 defaults: DATABASE.md:1369; proposed: CODE-PLAN Appendix A
    expected: dict[ErrorCode, tuple[str | None, str, str | None]] = {
        ErrorCode.SCHEMA_INVALID: ("content_error", "ai_correct", None),
        ErrorCode.FIELD_TOO_LONG: ("content_error", "ai_correct", None),
        ErrorCode.FIELD_TOO_SHORT: ("content_error", "ai_correct", None),
        ErrorCode.BANNED_TERM: ("content_error", "ai_correct", None),
        ErrorCode.BYTE_BUDGET_EXCEEDED: ("content_error", "ai_correct", None),
        ErrorCode.UNSUPPORTED_CLAIM: ("content_error", "ai_correct", None),
        ErrorCode.PATTERN_MISMATCH: ("content_error", "ai_correct", None),
        ErrorCode.KEYWORD_DUPLICATE: ("content_error", "ai_correct", "reformat"),
        ErrorCode.REQUIRED_FIELD_MISSING: ("missing_data", "user_action", None),
        ErrorCode.MISSING_ATTRIBUTE: ("missing_data", "user_action", None),
        ErrorCode.VALUE_NOT_ALLOWED: ("invalid_value", "user_action", None),
        ErrorCode.CROSS_FIELD_MISMATCH: ("invalid_value", "user_action", None),
        ErrorCode.CATEGORY_MISMATCH: ("category_mismatch", "user_action", None),
        ErrorCode.CATEGORY_NOT_MAPPED: (None, "system_failure", None),
        ErrorCode.IMAGE_NON_COMPLIANT: ("media_error", "user_action", None),
        ErrorCode.SKU_CONFLICT: ("identifier_error", "user_action", None),
        ErrorCode.IDENTIFIER_MISSING: ("identifier_error", "user_action", None),
        ErrorCode.IDENTIFIER_INVALID: ("identifier_error", "user_action", None),
        ErrorCode.IDENTIFIER_DUPLICATE: ("identifier_error", "user_action", None),
        ErrorCode.VARIATION_INVALID: ("variation_error", "user_action", "rebuild_variation"),
        ErrorCode.COMPLIANCE_DECLARATION_MISSING: ("compliance_error", "user_action", None),
        ErrorCode.AUTH_EXPIRED: ("account_permission", "user_action", None),
        ErrorCode.ACCOUNT_PERMISSION_DENIED: ("account_permission", "user_action", None),
        ErrorCode.RATE_LIMITED: ("marketplace_technical", "auto_retry", None),
        ErrorCode.MARKETPLACE_UNAVAILABLE: ("marketplace_technical", "auto_retry", None),
        ErrorCode.SUBMISSION_NOT_APPLIED: ("marketplace_technical", "auto_retry", None),
        ErrorCode.MARKETPLACE_UNCONFIRMED: ("marketplace_technical", "auto_retry", None),
        ErrorCode.API_REJECTED_PARTIAL: ("unknown", "user_action", None),
        ErrorCode.MARKETPLACE_UNKNOWN_ERROR: ("unknown", "user_action", None),
        ErrorCode.JSON_INVALID: (None, "auto_retry", None),
        ErrorCode.TIMEOUT: (None, "auto_retry", None),
        ErrorCode.TOKEN_LIMIT: (None, "auto_retry", None),
        ErrorCode.MODEL_UNAVAILABLE: (None, "auto_retry", None),
        ErrorCode.INTERNAL_ERROR: (None, "system_failure", None),
        ErrorCode.RETRY_EXHAUSTED: (None, "system_failure", None),
        ErrorCode.JOB_TIMEOUT: (None, "system_failure", None),
        ErrorCode.VALIDATION_FAILED: (None, "user_action", None),
        ErrorCode.GENERATION_FAILED: (None, "user_action", None),
        ErrorCode.PUBLISH_FAILED: (None, "user_action", None),
        ErrorCode.IMPORT_FAILED: (None, "user_action", None),
        ErrorCode.HEADER_MISSING_COLUMN: ("missing_data", "user_action", None),
        ErrorCode.HEADER_UNKNOWN_COLUMN: ("invalid_value", "user_action", None),
        ErrorCode.HEADER_DUPLICATE_COLUMN: ("invalid_value", "user_action", None),
        ErrorCode.DUPLICATE_SKU_IN_FILE: ("identifier_error", "user_action", None),
        ErrorCode.SELLER_SKU_CONFLICT: ("identifier_error", "user_action", None),
        ErrorCode.CRITICAL_FACT_MISSING: ("missing_data", "user_action", None),
        ErrorCode.FACT_NEEDS_CONFIRMATION: ("missing_data", "user_action", None),
        ErrorCode.FACT_SOURCE_FORBIDDEN: ("invalid_value", "user_action", None),
        ErrorCode.UNKNOWN_FACT_KEY: ("invalid_value", "user_action", None),
        ErrorCode.FACT_LEVEL_INVALID: ("invalid_value", "user_action", None),
        ErrorCode.PRICE_ABOVE_MRP: ("invalid_value", "user_action", None),
        ErrorCode.FACT_MISMATCH: ("invalid_value", "ai_correct", None),
        ErrorCode.VARIANT_MATRIX_INCOMPLETE: ("variation_error", "user_action", None),
        ErrorCode.MAKER_CHECKER_VIOLATION: (None, "user_action", None),
        ErrorCode.INVALID_STATUS_TRANSITION: (None, "user_action", None),
        ErrorCode.LISTING_PAUSED: (None, "user_action", None),
        ErrorCode.LISTING_ARCHIVED: (None, "user_action", None),
        ErrorCode.LISTING_BUSY: (None, "user_action", None),
        ErrorCode.SUBMISSION_ATTEMPTS_EXHAUSTED: (None, "user_action", None),
        ErrorCode.DUPLICATE_SUBMISSION_BLOCKED: (None, "user_action", None),
        ErrorCode.SUBMISSION_IN_FLIGHT: (None, "user_action", None),
        ErrorCode.FACTS_CHANGED_SINCE_APPROVAL: (None, "user_action", None),
        ErrorCode.BATCH_TOO_LARGE: (None, "user_action", None),
        ErrorCode.BULK_APPROVE_REQUIRES_VALIDATED: (None, "user_action", None),
        ErrorCode.BUDGET_EXCEEDED: (None, "system_failure", None),
        # S3 API codes (CODE-PLAN s3 T2; D2, D3, S15 hand-off)
        ErrorCode.UNAUTHENTICATED: (None, "user_action", None),
        ErrorCode.PERMISSION_DENIED: (None, "user_action", None),
        ErrorCode.NOT_FOUND: (None, "user_action", None),
        ErrorCode.METHOD_NOT_ALLOWED: (None, "user_action", None),
        ErrorCode.IDEMPOTENCY_KEY_REUSED: (None, "user_action", None),
        ErrorCode.STALE_VERSION: (None, "user_action", None),
        ErrorCode.PAYLOAD_TOO_LARGE: (None, "user_action", None),
        ErrorCode.OAUTH_STATE_INVALID: (None, "user_action", None),
        ErrorCode.NOT_IMPLEMENTED: (None, "system_failure", None),
        ErrorCode.LAST_OWNER_REQUIRED: (None, "user_action", None),
        ErrorCode.MARKETPLACE_ACCOUNT_CONFLICT: (None, "user_action", None),
    }
    for code, exp in expected.items():
        assert _meta_tuple(ERROR_CODE_META[code]) == exp, code.name


def test_http_codes_meta() -> None:
    # Every S3 API code is an HTTP-layer failure: no ErrorClass, no auto-fix, and only
    # NOT_IMPLEMENTED is a system failure (retrying the request will not help a user either way).
    for name in API_CODES:
        meta = ERROR_CODE_META[ErrorCode(name)]
        assert meta.error_class is None, name
        assert meta.fix_action is None, name
        expected = (
            RetryClass.SYSTEM_FAILURE if name == "NOT_IMPLEMENTED" else RetryClass.USER_ACTION
        )
        assert meta.retry_class is expected, name


def test_error_code_meta_is_read_only() -> None:
    assert isinstance(ERROR_CODE_META, MappingProxyType)
    try:
        ERROR_CODE_META[ErrorCode.INTERNAL_ERROR] = CodeMeta(  # type: ignore[index]
            None, RetryClass.SYSTEM_FAILURE
        )
        raise AssertionError("expected TypeError")
    except TypeError:
        pass


def test_codes_values_are_upper_snake() -> None:
    pat = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
    for m in ErrorCode:
        assert pat.match(m.value), m.value
