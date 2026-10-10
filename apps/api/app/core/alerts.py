"""Alert events (S23 AC11; PLAN D7; C2, C3): one dedupe gates every sink, and every kind reaches
Sentry.

`emit_alert(kind, key=, window_start=, now=, claim=, cap=, cap_claim=, slack=, **fields)`:

1. `fields` must be names from `ALERT_FIELDS` with id-shaped values (`str` of at most 128
   characters, `int`, `Decimal`, `UUID`): never listing text, SQL, tokens or URLs. Anything else
   is a `TypeError` (a programming error, raised before any sink).
2. `claim(kind, key, window_start)` is the dedupe: False (already emitted in this window) logs
   one DEBUG `alert.suppressed` and emits nothing.
3. With a `cap` (the dead-letter storm, D7), `cap_claim(cap.bucket, window)` counts this event in
   the cap's window; above `cap.limit` it logs DEBUG `alert.suppressed reason=cap` only. The
   periodic check later emits one summary per closed over-cap window.
4. Else: one WARNING log event `alert.<kind>` (below the Sentry logging integration's event
   level, so no second Sentry event), one Sentry message with the fingerprint
   `["alert", kind, key]` (dead letters: `["alert", kind, job_kind, error_code]`, grouped, never
   the job id), and a Slack post for `SLACK_KINDS`. A failing sink is logged by class name and
   never raised: an alert never fails the job or check that raised it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal, Protocol, get_args
from uuid import UUID

import sentry_sdk

from app.core.logging import get_logger

log = get_logger(__name__)

AlertKind = Literal[
    "token_refresh_failed", "job_dead_lettered", "job_error_rate", "llm_budget", "queue_latency"
]
ALERT_KINDS: Final[frozenset[str]] = frozenset(get_args(AlertKind))
ALERT_FIELDS: Final = frozenset(
    {
        "tenant_id",
        "account_id",
        "job_id",
        "job_kind",
        "queue",
        "marketplace",
        "error_code",
        "threshold_pct",
        "scope",
        "count",
        "total",
        "rate",
        "spend_usd",
        "limit_usd",
        "oldest_s",
    }
)
SLACK_KINDS: Final[frozenset[str]] = frozenset(
    {"llm_budget", "queue_latency", "token_refresh_failed"}
)
# kinds whose Sentry fingerprint groups by these fields instead of the dedupe key
FINGERPRINT: Final[Mapping[str, tuple[str, ...]]] = {
    "job_dead_lettered": ("job_kind", "error_code")
}
MAX_TEXT: Final = 128
DEAD_LETTER_CAP: Final = 20
DEAD_LETTER_CAP_WINDOW: Final = timedelta(minutes=15)

FieldValue = str | int | Decimal | UUID
Claim = Callable[[str, str, datetime], Awaitable[bool]]
CapClaim = Callable[[str, datetime], Awaitable[int]]


class SlackSender(Protocol):
    async def post(self, kind: str, fields: Mapping[str, str]) -> None: ...


@dataclass(frozen=True, slots=True)
class AlertCap:
    bucket: str
    limit: int
    window: timedelta


def floor_window(now: datetime, size: timedelta) -> datetime:
    """The start of the `size` window `now` falls in (UTC, aligned to the epoch)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    seconds = int(size.total_seconds())
    epoch = int(now.astimezone(UTC).timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=UTC)


def day_window(now: datetime) -> datetime:
    return floor_window(now, timedelta(days=1))


def dead_letter_cap(job_kind: str) -> AlertCap:
    return AlertCap(
        f"alert:job_dead_lettered:cap:{job_kind}", DEAD_LETTER_CAP, DEAD_LETTER_CAP_WINDOW
    )


def _checked(fields: Mapping[str, object]) -> dict[str, str]:
    unknown = sorted(set(fields) - ALERT_FIELDS)
    if unknown:
        raise TypeError(f"unknown alert field(s) {unknown}")
    out: dict[str, str] = {}
    for name, value in fields.items():
        if isinstance(value, bool) or not isinstance(value, (str, int, Decimal, UUID)):
            raise TypeError(f"alert field {name} must be an id, a code or a number")
        text = str(value)
        if len(text) > MAX_TEXT:
            raise TypeError(f"alert field {name} is longer than {MAX_TEXT} characters")
        out[name] = text
    return out


def fingerprint(kind: str, key: str, fields: Mapping[str, str]) -> list[str]:
    by = FINGERPRINT.get(kind)
    if by is None:
        return ["alert", kind, key]
    return ["alert", kind, *(fields.get(name, "") for name in by)]


async def emit_alert(
    kind: AlertKind,
    *,
    key: str,
    window_start: datetime,
    now: datetime,
    claim: Claim,
    cap: AlertCap | None = None,
    cap_claim: CapClaim | None = None,
    slack: SlackSender | None = None,
    summary: bool = False,
    **fields: Any,
) -> bool:
    """Emit one alert event (module docstring). True when it reached the sinks."""
    if kind not in ALERT_KINDS:
        raise TypeError(f"unknown alert kind {kind!r}")
    values = _checked(fields)
    if not await claim(kind, key, window_start):
        log.debug("alert.suppressed", kind=kind, reason="dedupe")
        return False
    if cap is not None:
        if cap_claim is None:
            raise TypeError("a cap needs its cap_claim")
        n = await cap_claim(cap.bucket, floor_window(now, cap.window))
        if n > cap.limit:
            log.debug("alert.suppressed", kind=kind, reason="cap")
            return False
    log.warning(f"alert.{kind}", **values)
    marks = fingerprint(kind, key, values)
    if summary:  # one per closed over-cap window: `["alert", kind, job_kind, "summary"]`
        marks = ["alert", kind, values.get("job_kind", ""), "summary"]
    try:
        with sentry_sdk.new_scope() as scope:
            scope.fingerprint = marks
            for name, value in values.items():
                scope.set_tag(name, value)
            scope.set_tag("alert_kind", kind)
            sentry_sdk.capture_message(f"alert.{kind}", level="warning")
    except Exception as exc:  # a sink never fails the caller
        log.warning("alert.sentry_failed", kind=kind, error_class=type(exc).__name__)
    if slack is not None and kind in SLACK_KINDS:
        try:
            await slack.post(kind, values)
        except Exception as exc:
            log.warning("alert.slack_failed", kind=kind, error_class=type(exc).__name__)
    return True
