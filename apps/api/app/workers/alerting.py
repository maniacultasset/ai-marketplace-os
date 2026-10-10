"""The worker's alert sinks wired to their stores (S23 T23-T24): the dedupe claim and the cap
counter on `rate_limit_counters`, Slack from `ALERT_SLACK_WEBHOOK`. Called only after the write
that caused the alert has committed. An alert that cannot be sent (its claim's database call
failed) is logged at ERROR by class name and never raised: it must not fail the job, the refresh
or the periodic check that raised it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.alerts import AlertCap, AlertKind, emit_alert
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.slack import SlackSender
from app.repositories import rate_limits

log = get_logger(__name__)


async def alert(
    kind: AlertKind,
    *,
    key: str,
    window_start: datetime,
    now: datetime | None = None,
    cap: AlertCap | None = None,
    summary: bool = False,
    **fields: Any,
) -> bool:
    try:
        return await emit_alert(
            kind,
            key=key,
            window_start=window_start,
            now=now or datetime.now(UTC),
            claim=rate_limits.claim_alert,
            cap=cap,
            cap_claim=rate_limits.claim_cap if cap is not None else None,
            slack=SlackSender(get_settings().alert_slack_webhook),
            summary=summary,
            **fields,
        )
    except Exception as exc:
        log.error("alert.failed", kind=kind, error_class=type(exc).__name__)
        return False
