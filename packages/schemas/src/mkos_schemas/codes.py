"""Error and image issue codes (single registry)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from .enums import ErrorClass, FixAction, RetryClass


class ErrorCode(StrEnum):
    """Every machine-readable error code in v1.0.0."""

    # --- adapter ---
    ACCOUNT_PERMISSION_DENIED = "ACCOUNT_PERMISSION_DENIED"

    API_REJECTED_PARTIAL = "API_REJECTED_PARTIAL"

    AUTH_EXPIRED = "AUTH_EXPIRED"

    # --- rules ---
    BANNED_TERM = "BANNED_TERM"

    # --- api ---
    BATCH_TOO_LARGE = "BATCH_TOO_LARGE"

    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"

    BULK_APPROVE_REQUIRES_VALIDATED = "BULK_APPROVE_REQUIRES_VALIDATED"

    # --- rules ---
    BYTE_BUDGET_EXCEEDED = "BYTE_BUDGET_EXCEEDED"

    CATEGORY_MISMATCH = "CATEGORY_MISMATCH"

    # --- adapter ---
    CATEGORY_NOT_MAPPED = "CATEGORY_NOT_MAPPED"

    COMPLIANCE_DECLARATION_MISSING = "COMPLIANCE_DECLARATION_MISSING"

    # --- facts ---
    CRITICAL_FACT_MISSING = "CRITICAL_FACT_MISSING"

    # --- rules ---
    CROSS_FIELD_MISMATCH = "CROSS_FIELD_MISMATCH"

    # --- schema ---
    DUPLICATE_SKU_IN_FILE = "DUPLICATE_SKU_IN_FILE"

    # --- api ---
    DUPLICATE_SUBMISSION_BLOCKED = "DUPLICATE_SUBMISSION_BLOCKED"

    FACTS_CHANGED_SINCE_APPROVAL = "FACTS_CHANGED_SINCE_APPROVAL"

    # --- facts ---
    FACT_LEVEL_INVALID = "FACT_LEVEL_INVALID"

    FACT_MISMATCH = "FACT_MISMATCH"

    FACT_NEEDS_CONFIRMATION = "FACT_NEEDS_CONFIRMATION"

    FACT_SOURCE_FORBIDDEN = "FACT_SOURCE_FORBIDDEN"

    # --- rules ---
    FIELD_TOO_LONG = "FIELD_TOO_LONG"

    FIELD_TOO_SHORT = "FIELD_TOO_SHORT"

    # --- summary ---
    GENERATION_FAILED = "GENERATION_FAILED"

    # --- schema ---
    HEADER_DUPLICATE_COLUMN = "HEADER_DUPLICATE_COLUMN"

    HEADER_MISSING_COLUMN = "HEADER_MISSING_COLUMN"

    HEADER_UNKNOWN_COLUMN = "HEADER_UNKNOWN_COLUMN"

    # --- adapter ---
    IDENTIFIER_DUPLICATE = "IDENTIFIER_DUPLICATE"

    IDENTIFIER_INVALID = "IDENTIFIER_INVALID"

    IDENTIFIER_MISSING = "IDENTIFIER_MISSING"

    # --- vision ---
    IMAGE_NON_COMPLIANT = "IMAGE_NON_COMPLIANT"

    # --- summary ---
    IMPORT_FAILED = "IMPORT_FAILED"

    # --- system ---
    INTERNAL_ERROR = "INTERNAL_ERROR"

    # --- api ---
    INVALID_STATUS_TRANSITION = "INVALID_STATUS_TRANSITION"

    # --- system ---
    JOB_TIMEOUT = "JOB_TIMEOUT"

    # --- ai ---
    JSON_INVALID = "JSON_INVALID"

    # --- rules ---
    KEYWORD_DUPLICATE = "KEYWORD_DUPLICATE"

    # --- api ---
    LISTING_ARCHIVED = "LISTING_ARCHIVED"

    LISTING_BUSY = "LISTING_BUSY"

    LISTING_PAUSED = "LISTING_PAUSED"

    MAKER_CHECKER_VIOLATION = "MAKER_CHECKER_VIOLATION"

    # --- adapter ---
    MARKETPLACE_UNAVAILABLE = "MARKETPLACE_UNAVAILABLE"

    MARKETPLACE_UNCONFIRMED = "MARKETPLACE_UNCONFIRMED"

    MARKETPLACE_UNKNOWN_ERROR = "MARKETPLACE_UNKNOWN_ERROR"

    # --- rules ---
    MISSING_ATTRIBUTE = "MISSING_ATTRIBUTE"

    # --- ai ---
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"

    # --- rules ---
    PATTERN_MISMATCH = "PATTERN_MISMATCH"

    # --- facts ---
    PRICE_ABOVE_MRP = "PRICE_ABOVE_MRP"

    # --- summary ---
    PUBLISH_FAILED = "PUBLISH_FAILED"

    # --- adapter ---
    RATE_LIMITED = "RATE_LIMITED"

    # --- schema ---
    REQUIRED_FIELD_MISSING = "REQUIRED_FIELD_MISSING"

    # --- system ---
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"

    # --- schema ---
    SCHEMA_INVALID = "SCHEMA_INVALID"

    # --- adapter ---
    SELLER_SKU_CONFLICT = "SELLER_SKU_CONFLICT"

    SKU_CONFLICT = "SKU_CONFLICT"

    # --- api ---
    SUBMISSION_ATTEMPTS_EXHAUSTED = "SUBMISSION_ATTEMPTS_EXHAUSTED"

    SUBMISSION_IN_FLIGHT = "SUBMISSION_IN_FLIGHT"

    # --- adapter ---
    SUBMISSION_NOT_APPLIED = "SUBMISSION_NOT_APPLIED"

    # --- ai ---
    TIMEOUT = "TIMEOUT"

    TOKEN_LIMIT = "TOKEN_LIMIT"

    # --- facts ---
    UNKNOWN_FACT_KEY = "UNKNOWN_FACT_KEY"

    # --- rules ---
    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"

    # --- summary ---
    VALIDATION_FAILED = "VALIDATION_FAILED"

    # --- rules ---
    VALUE_NOT_ALLOWED = "VALUE_NOT_ALLOWED"

    # --- facts ---
    VARIANT_MATRIX_INCOMPLETE = "VARIANT_MATRIX_INCOMPLETE"

    # --- adapter ---
    VARIATION_INVALID = "VARIATION_INVALID"


class ImageIssueCode(StrEnum):
    BACKGROUND_OFF = "BACKGROUND_OFF"
    RESOLUTION_LOW = "RESOLUTION_LOW"
    TEXT_DETECTED = "TEXT_DETECTED"
    WATERMARK = "WATERMARK"
    FILL_RATIO_LOW = "FILL_RATIO_LOW"
    WRONG_ASPECT = "WRONG_ASPECT"
    FORMAT_UNSUPPORTED = "FORMAT_UNSUPPORTED"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    CORRUPT = "CORRUPT"
    LOGO_PROHIBITED = "LOGO_PROHIBITED"


@dataclass(frozen=True, slots=True)
class CodeMeta:
    error_class: ErrorClass | None
    retry_class: RetryClass
    fix_action: FixAction | None = None


def _meta() -> dict[ErrorCode, CodeMeta]:
    raw: dict[
        ErrorCode,
        CodeMeta | tuple[ErrorClass | None, RetryClass, FixAction | None],
    ] = {
        ErrorCode.ACCOUNT_PERMISSION_DENIED: CodeMeta(ErrorClass.ACCOUNT_PERMISSION, RetryClass.USER_ACTION),
        ErrorCode.API_REJECTED_PARTIAL: CodeMeta(ErrorClass.UNKNOWN, RetryClass.USER_ACTION),
        ErrorCode.AUTH_EXPIRED: CodeMeta(ErrorClass.ACCOUNT_PERMISSION, RetryClass.USER_ACTION),
        ErrorCode.BANNED_TERM: CodeMeta(ErrorClass.CONTENT_ERROR, RetryClass.AI_CORRECT),
        ErrorCode.BATCH_TOO_LARGE: CodeMeta(None, RetryClass.USER_ACTION),
        ErrorCode.BUDGET_EXCEEDED: CodeMeta(None, RetryClass.SYSTEM_FAILURE),
        ErrorCode.BULK_APPROVE_REQUIRES_VALIDATED: CodeMeta(None, RetryClass.USER_ACTION),
        ErrorCode.BYTE_BUDGET_EXCEEDED: CodeMeta(ErrorClass.CONTENT_ERROR, RetryClass.AI_CORRECT),
        ErrorCode.CATEGORY_MISMATCH: CodeMeta(ErrorClass.CATEGORY_MISMATCH, RetryClass.USER_ACTION),
        ErrorCode.CATEGORY_NOT_MAPPED: CodeMeta(None, RetryClass.SYSTEM_FAILURE),
        ErrorCode.COMPLIANCE_DECLARATION_MISSING: CodeMeta(ErrorClass.COMPLIANCE_ERROR, RetryClass.USER_ACTION),
        ErrorCode.CRITICAL_FACT_MISSING: CodeMeta(ErrorClass.MISSING_DATA, RetryClass.USER_ACTION),
        ErrorCode.CROSS_FIELD_MISMATCH: CodeMeta(ErrorClass.INVALID_VALUE, RetryClass.USER_ACTION),
        ErrorCode.DUPLICATE_SKU_IN_FILE: CodeMeta(ErrorClass.IDENTIFIER_ERROR, RetryClass.USER_ACTION),
        ErrorCode.DUPLICATE_SUBMISSION_BLOCKED: CodeMeta(None, RetryClass.USER_ACTION),
        ErrorCode.FACTS_CHANGED_SINCE_APPROVAL: CodeMeta(None, RetryClass.USER_ACTION),
        ErrorCode.FACT_LEVEL_INVALID: CodeMeta(ErrorClass.INVALID_VALUE, RetryClass.USER_ACTION),
        ErrorCode.FACT_MISMATCH: CodeMeta(ErrorClass.INVALID_VALUE, RetryClass.AI_CORRECT),
        ErrorCode.FACT_NEEDS_CONFIRMATION: CodeMeta(ErrorClass.MISSING_DATA, RetryClass.USER_ACTION),
        ErrorCode.FACT_SOURCE_FORBIDDEN: CodeMeta(ErrorClass.INVALID_VALUE, RetryClass.USER_ACTION),
        ErrorCode.FIELD_TOO_LONG: CodeMeta(ErrorClass.CONTENT_ERROR, RetryClass.AI_CORRECT),
        ErrorCode.FIELD_TOO_SHORT: CodeMeta(ErrorClass.CONTENT_ERROR, RetryClass.AI_CORRECT),
        ErrorCode.GENERATION_FAILED: CodeMeta(None, RetryClass.USER_ACTION),
        ErrorCode.HEADER_DUPLICATE_COLUMN: CodeMeta(ErrorClass.INVALID_VALUE, RetryClass.USER_ACTION),
        ErrorCode.HEADER_MISSING_COLUMN: CodeMeta(ErrorClass.MISSING_DATA, RetryClass.USER_ACTION),
        ErrorCode.HEADER_UNKNOWN_COLUMN: CodeMeta(ErrorClass.INVALID_VALUE, RetryClass.USER_ACTION),
        ErrorCode.IDENTIFIER_DUPLICATE: CodeMeta(ErrorClass.IDENTIFIER_ERROR, RetryClass.USER_ACTION),
        ErrorCode.IDENTIFIER_INVALID: CodeMeta(ErrorClass.IDENTIFIER_ERROR, RetryClass.USER_ACTION),
        ErrorCode.IDENTIFIER_MISSING: CodeMeta(ErrorClass.IDENTIFIER_ERROR, RetryClass.USER_ACTION),
        ErrorCode.IMAGE_NON_COMPLIANT: CodeMeta(ErrorClass.MEDIA_ERROR, RetryClass.USER_ACTION),
        ErrorCode.IMPORT_FAILED: CodeMeta(None, RetryClass.USER_ACTION),
        ErrorCode.INTERNAL_ERROR: CodeMeta(None, RetryClass.SYSTEM_FAILURE),
        ErrorCode.INVALID_STATUS_TRANSITION: CodeMeta(None, RetryClass.USER_ACTION),
        ErrorCode.JOB_TIMEOUT: CodeMeta(None, RetryClass.SYSTEM_FAILURE),
        ErrorCode.JSON_INVALID: CodeMeta(None, RetryClass.AUTO_RETRY),
        ErrorCode.KEYWORD_DUPLICATE: (ErrorClass.CONTENT_ERROR, RetryClass.AI_CORRECT, FixAction.REFORMAT),
        ErrorCode.LISTING_ARCHIVED: CodeMeta(None, RetryClass.USER_ACTION),
        ErrorCode.LISTING_BUSY: CodeMeta(None, RetryClass.USER_ACTION),
        ErrorCode.LISTING_PAUSED: CodeMeta(None, RetryClass.USER_ACTION),
        ErrorCode.MAKER_CHECKER_VIOLATION: CodeMeta(None, RetryClass.USER_ACTION),
        ErrorCode.MARKETPLACE_UNAVAILABLE: CodeMeta(ErrorClass.MARKETPLACE_TECHNICAL, RetryClass.AUTO_RETRY),
        ErrorCode.MARKETPLACE_UNCONFIRMED: CodeMeta(ErrorClass.MARKETPLACE_TECHNICAL, RetryClass.AUTO_RETRY),
        ErrorCode.MARKETPLACE_UNKNOWN_ERROR: CodeMeta(ErrorClass.UNKNOWN, RetryClass.USER_ACTION),
        ErrorCode.MISSING_ATTRIBUTE: CodeMeta(ErrorClass.MISSING_DATA, RetryClass.USER_ACTION),
        ErrorCode.MODEL_UNAVAILABLE: CodeMeta(None, RetryClass.AUTO_RETRY),
        ErrorCode.PATTERN_MISMATCH: CodeMeta(ErrorClass.CONTENT_ERROR, RetryClass.AI_CORRECT),
        ErrorCode.PRICE_ABOVE_MRP: CodeMeta(ErrorClass.INVALID_VALUE, RetryClass.USER_ACTION),
        ErrorCode.PUBLISH_FAILED: CodeMeta(None, RetryClass.USER_ACTION),
        ErrorCode.RATE_LIMITED: CodeMeta(ErrorClass.MARKETPLACE_TECHNICAL, RetryClass.AUTO_RETRY),
        ErrorCode.REQUIRED_FIELD_MISSING: CodeMeta(ErrorClass.MISSING_DATA, RetryClass.USER_ACTION),
        ErrorCode.RETRY_EXHAUSTED: CodeMeta(None, RetryClass.SYSTEM_FAILURE),
        ErrorCode.SCHEMA_INVALID: CodeMeta(ErrorClass.CONTENT_ERROR, RetryClass.AI_CORRECT),
        ErrorCode.SELLER_SKU_CONFLICT: CodeMeta(ErrorClass.IDENTIFIER_ERROR, RetryClass.USER_ACTION),
        ErrorCode.SKU_CONFLICT: CodeMeta(ErrorClass.IDENTIFIER_ERROR, RetryClass.USER_ACTION),
        ErrorCode.SUBMISSION_ATTEMPTS_EXHAUSTED: CodeMeta(None, RetryClass.USER_ACTION),
        ErrorCode.SUBMISSION_IN_FLIGHT: CodeMeta(None, RetryClass.USER_ACTION),
        ErrorCode.SUBMISSION_NOT_APPLIED: CodeMeta(ErrorClass.MARKETPLACE_TECHNICAL, RetryClass.AUTO_RETRY),
        ErrorCode.TIMEOUT: CodeMeta(None, RetryClass.AUTO_RETRY),
        ErrorCode.TOKEN_LIMIT: CodeMeta(None, RetryClass.AUTO_RETRY),
        ErrorCode.UNKNOWN_FACT_KEY: CodeMeta(ErrorClass.INVALID_VALUE, RetryClass.USER_ACTION),
        ErrorCode.UNSUPPORTED_CLAIM: CodeMeta(ErrorClass.CONTENT_ERROR, RetryClass.AI_CORRECT),
        ErrorCode.VALIDATION_FAILED: CodeMeta(None, RetryClass.USER_ACTION),
        ErrorCode.VALUE_NOT_ALLOWED: CodeMeta(ErrorClass.INVALID_VALUE, RetryClass.USER_ACTION),
        ErrorCode.VARIANT_MATRIX_INCOMPLETE: CodeMeta(ErrorClass.VARIATION_ERROR, RetryClass.USER_ACTION),
        ErrorCode.VARIATION_INVALID: (ErrorClass.VARIATION_ERROR, RetryClass.USER_ACTION, FixAction.REBUILD_VARIATION),
    }
    out: dict[ErrorCode, CodeMeta] = {}
    for code, item in raw.items():
        if isinstance(item, CodeMeta):
            out[code] = item
        else:
            ec, rc, fa = item
            out[code] = CodeMeta(ec, rc, fa)
    return out


ERROR_CODE_META: Mapping[ErrorCode, CodeMeta] = MappingProxyType(_meta())
