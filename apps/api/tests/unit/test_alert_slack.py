"""S23 T22 (AC12; D8): the Slack sink: one POST, ids only, never raises, never logs its URL."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

URL = "https://hooks.slack.example/services/T000/B000/Zq9webhookcanary"
FIELDS = {"queue": "default", "oldest_s": "600"}


def _sender(handler: Any, url: str | None = URL) -> Any:
    from app.core.slack import SlackSender

    return SlackSender(url, transport=httpx.MockTransport(handler))


async def test_alert_slack_one_post() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    await _sender(handler).post("queue_latency", FIELDS)
    [request] = seen
    assert request.method == "POST" and str(request.url) == URL
    assert json.loads(request.content) == {"text": "alert.queue_latency oldest_s=600 queue=default"}


async def test_alert_slack_empty_no_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no call without a URL")

    await _sender(handler, url=None).post("queue_latency", FIELDS)
    await _sender(handler, url="").post("queue_latency", FIELDS)


async def test_alert_slack_http_scheme_refused(log_lines: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an http:// webhook is never called")

    capture = log_lines()
    await _sender(handler, url="http://hooks.slack.example/x").post("llm_budget", FIELDS)
    assert "alert.slack_refused" in [line["message"] for line in capture.lines()]


async def test_alert_slack_500_logged_not_raised(log_lines: Any) -> None:
    capture = log_lines()
    await _sender(lambda r: httpx.Response(500)).post("llm_budget", FIELDS)
    [line] = [x for x in capture.lines() if x["message"] == "alert.slack_failed"]
    assert line["status"] == 500


@pytest.mark.parametrize(
    ("error", "name"),
    [(httpx.ReadTimeout("t"), "ReadTimeout"), (httpx.ConnectError("c"), "ConnectError")],
    ids=["timeout", "connect_refused"],
)
async def test_alert_slack_transport_error_logged(
    log_lines: Any, error: Exception, name: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    capture = log_lines()
    await _sender(handler).post("llm_budget", FIELDS)
    [line] = [x for x in capture.lines() if x["message"] == "alert.slack_failed"]
    assert line["error_class"] == name


async def test_alert_slack_url_never_logged(log_lines: Any, sentry_events: Any) -> None:
    capture = log_lines()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    sender = _sender(handler)
    await sender.post("llm_budget", FIELDS)
    await _sender(lambda r: httpx.Response(503)).post("llm_budget", FIELDS)
    assert "Zq9webhookcanary" not in capture.text()
    assert "Zq9webhookcanary" not in sentry_events.serialized()
    assert "Zq9webhookcanary" not in repr(sender)


async def test_alert_slack_body_ids_only() -> None:
    bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content.decode())
        return httpx.Response(200)

    await _sender(handler).post(
        "token_refresh_failed", {"account_id": "a1", "error_code": "AUTH_EXPIRED"}
    )
    assert (
        json.loads(bodies[0])["text"]
        == "alert.token_refresh_failed account_id=a1 error_code=AUTH_EXPIRED"
    )
