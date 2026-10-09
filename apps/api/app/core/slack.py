"""The Slack alert sink (S23 AC12; PLAN D8; ENV.md `ALERT_SLACK_WEBHOOK`).

`SlackSender(url, transport=None).post(kind, fields)`: one POST of `{"text": "alert.<kind> k=v ..."}`
(ids and numbers only, as `emit_alert` checked them), 3 s timeout, one attempt. No URL: nothing is
sent. A URL that is not `https` is refused (`alert.slack_refused`, nothing sent). A transport error
or a non-2xx answer logs `alert.slack_failed` with the status or the exception class and is never
raised. The webhook URL is a secret: it is never logged, and no log line here names it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import httpx

from app.core.logging import get_logger

log = get_logger(__name__)
TIMEOUT_S: Final = 3.0


class SlackSender:
    def __init__(
        self, url: str | None, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._url = url or None
        self._transport = transport

    def __repr__(self) -> str:
        return "SlackSender()"

    async def post(self, kind: str, fields: Mapping[str, str]) -> None:
        if self._url is None:
            return
        if not self._url.startswith("https://"):
            log.warning("alert.slack_refused", kind=kind, reason="not_https")
            return
        text = " ".join([f"alert.{kind}", *(f"{k}={v}" for k, v in sorted(fields.items()))])
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=TIMEOUT_S,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(self._url, json={"text": text})
        except Exception as exc:  # one attempt; an alert never fails its caller
            log.warning("alert.slack_failed", kind=kind, error_class=type(exc).__name__)
            return
        if not 200 <= response.status_code < 300:
            log.warning("alert.slack_failed", kind=kind, status=response.status_code)
