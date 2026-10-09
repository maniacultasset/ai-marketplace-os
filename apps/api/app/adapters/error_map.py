"""The marketplace error mapper (DATABASE.md:546-557; spec L10531; invariant 8; S20 T3).

Pure: no HTTP, no socket, no database. `ErrorMapper(rows)` holds one marketplace's enabled
`marketplace_error_map` rows (read by `ErrorMapRepository`) and turns a raw marketplace issue
into the one `ErrorItem` shape:

- lookup: the issue's code first, then each of its categories in order; the highest enabled
  version wins. No row -> `MARKETPLACE_UNKNOWN_ERROR`, class `unknown`, `user_action`, and the
  message is the raw marketplace text.
- a mapped row: `message` = its `plain_text` rendered with the three literal placeholders
  `{value}`, `{field}`, `{marketplace}` (plain `str.replace`, never `str.format`: a template can
  never reach an attribute or a format spec) + " Marketplace message: " + the raw text, cut to
  1000; `suggestion` = `seller_action`. A row's `fix_action` is reported, never executed here.
- `raw_code` is kept (cut to 128). Raw text is scrubbed of the live token (`secrets`), LWA token
  shapes and the usual credential patterns before it is stored anywhere.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import (
    ErrorClass,
    ErrorSource,
    FixAction,
    Marketplace,
    RetryClass,
    Severity,
)
from mkos_schemas.errors import ErrorItem

from app.core.redaction import redact_text

MESSAGE_MAX: Final = 1000
RAW_CODE_MAX: Final = 128
EMPTY_MESSAGE: Final = "Marketplace error"
MARKETPLACE_PREFIX: Final = " Marketplace message: "
REDACTED: Final = "[REDACTED]"
PLACEHOLDERS: Final = ("{value}", "{field}", "{marketplace}")
_LWA_TOKEN: Final = re.compile(r"Atz[ar]\|[^\s\"'<>,;]{1,4096}")
_RETRYABLE: Final = frozenset({RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT})


@dataclass(frozen=True)
class ErrorMapRow:
    marketplace: str
    match_on: str
    raw_value: str
    version: int
    error_code: ErrorCode
    error_class: ErrorClass
    retry_class: RetryClass
    severity: Severity
    auto_fixable: bool
    fix_action: FixAction | None
    field_hint: str | None
    plain_text: str
    seller_action: str | None
    enabled: bool = True


def render_plain_text(template: str, *, value: str | None, field: str, marketplace: str) -> str:
    """The three placeholders, replaced in one left-to-right pass: a substituted value is never
    scanned again, and any other `{...}` stays literal."""
    values = {"{value}": value or "", "{field}": field, "{marketplace}": marketplace}
    out: list[str] = []
    i = 0
    while i < len(template):
        for token in PLACEHOLDERS:
            if template.startswith(token, i):
                out.append(values[token])
                i += len(token)
                break
        else:
            out.append(template[i])
            i += 1
    return "".join(out)


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    """Raw marketplace text with the live token values, LWA token shapes and credential
    patterns removed."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    text = _LWA_TOKEN.sub(REDACTED, text)
    return redact_text(text)


class ErrorMapper:
    def __init__(self, rows: Iterable[ErrorMapRow]) -> None:
        self._best: dict[tuple[str, str], ErrorMapRow] = {}
        marketplaces: set[str] = set()
        for row in rows:
            marketplaces.add(row.marketplace)
            if not row.enabled:
                continue
            key = (row.match_on, row.raw_value)
            held = self._best.get(key)
            if held is None or row.version > held.version:
                self._best[key] = row
        if len(marketplaces) > 1:
            raise ValueError("an ErrorMapper holds one marketplace's rows")

    def lookup(self, code: str, categories: Sequence[str] = ()) -> ErrorMapRow | None:
        row = self._best.get(("code", code))
        if row is not None:
            return row
        for category in categories:
            row = self._best.get(("category", category))
            if row is not None:
                return row
        return None

    def to_item(
        self,
        *,
        raw_code: str,
        raw_message: str,
        field: str | None,
        marketplace: Marketplace | str = Marketplace.AMAZON,
        value: str | None = None,
        categories: Sequence[str] = (),
        secrets: Iterable[str] = (),
    ) -> ErrorItem:
        market = Marketplace(marketplace)
        source = ErrorSource(f"{market.value}_adapter")
        code_text = raw_code.strip()[:RAW_CODE_MAX] or None
        raw_text = scrub(raw_message.strip(), secrets) or EMPTY_MESSAGE
        row = self.lookup(raw_code.strip(), categories) if code_text else None
        if row is None:
            code = ErrorCode.MARKETPLACE_UNKNOWN_ERROR
            return ErrorItem(
                field=field or "listing",
                error_code=code,
                message=raw_text[:MESSAGE_MAX],
                value_submitted=value,
                source=source,
                retryable=False,
                marketplace=market,
                raw_code=code_text,
                error_class=ErrorClass.UNKNOWN,
                retry_class=ERROR_CODE_META[code].retry_class,
            )
        path = field or row.field_hint or "listing"
        summary = render_plain_text(row.plain_text, value=value, field=path, marketplace=market)
        return ErrorItem(
            field=path,
            error_code=row.error_code,
            severity=row.severity,
            message=(summary + MARKETPLACE_PREFIX + raw_text)[:MESSAGE_MAX],
            value_submitted=value,
            suggestion=row.seller_action or None,
            source=source,
            retryable=row.retry_class in _RETRYABLE,
            marketplace=market,
            raw_code=code_text,
            error_class=row.error_class,
            retry_class=row.retry_class,
            auto_fixable=row.auto_fixable,
            fix_action=row.fix_action,
        )
