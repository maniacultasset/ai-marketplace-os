"""S23 T20 (AC11; D7, C2): `emit_alert` — one log event and one Sentry message per condition,
a dedupe that silences every sink, a per-kind cap for dead-letter storms, ids-only payloads."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

NOW = datetime(2026, 10, 9, 12, 7, tzinfo=UTC)
TENANT = uuid.UUID("0b1c2d3e-0000-4000-8000-00000000aaaa")
FIELDS: dict[str, dict[str, Any]] = {
    "token_refresh_failed": {
        "tenant_id": TENANT,
        "account_id": uuid.UUID("0b1c2d3e-0000-4000-8000-00000000bbbb"),
        "marketplace": "amazon",
        "error_code": "AUTH_EXPIRED",
    },
    "job_dead_lettered": {
        "tenant_id": TENANT,
        "job_id": uuid.UUID("0b1c2d3e-0000-4000-8000-00000000cccc"),
        "job_kind": "bulk_import",
        "error_code": "JOB_TIMEOUT",
    },
    "job_error_rate": {"job_kind": "bulk_import", "count": 5, "total": 20, "rate": Decimal("0.25")},
    "llm_budget": {
        "scope": "tenant",
        "tenant_id": TENANT,
        "threshold_pct": 80,
        "spend_usd": Decimal("2.000000"),
        "limit_usd": Decimal("2.50"),
    },
    "queue_latency": {"queue": "default", "oldest_s": 600},
}


class Claims:
    """In-memory stand-ins for the `rate_limit_counters` claims."""

    def __init__(self) -> None:
        self.seen: Counter[tuple[str, str, datetime]] = Counter()
        self.caps: Counter[tuple[str, datetime]] = Counter()

    async def claim(self, kind: str, key: str, window: datetime) -> bool:
        self.seen[(kind, key, window)] += 1
        return self.seen[(kind, key, window)] == 1

    async def cap(self, bucket: str, window: datetime) -> int:
        self.caps[(bucket, window)] += 1
        return self.caps[(bucket, window)]


class FakeSlack:
    def __init__(self, fail: bool = False) -> None:
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.fail = fail

    async def post(self, kind: str, fields: Any) -> None:
        if self.fail:
            raise RuntimeError("slack down")
        self.posts.append((kind, dict(fields)))


@pytest.fixture
def sentry(sentry_events: Any) -> Any:
    from app.core.config import Settings
    from app.core.observability import init_sentry

    init_sentry(Settings(_env_file=None, app_env="local", app_role="shell", sentry_dsn="https://k@o0.ingest.sentry.io/1"))  # fmt: skip
    return sentry_events


def _flush() -> None:
    import sentry_sdk

    sentry_sdk.get_client().flush(timeout=2)


def _alerts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if str(e.get("message", "")).startswith("alert.")]


async def _emit(kind: str, claims: Claims, **over: Any) -> bool:
    from app.core.alerts import emit_alert

    fields = {**FIELDS[kind], **over.pop("fields", {})}
    return await emit_alert(
        kind,  # type: ignore[arg-type]
        key=over.pop("key", "k1"),
        window_start=over.pop("window_start", NOW),
        now=NOW,
        claim=claims.claim,
        **over,
        **fields,
    )


@pytest.mark.parametrize("kind", sorted(FIELDS))
async def test_alert_events_one_log_one_sentry(sentry: Any, log_lines: Any, kind: str) -> None:
    capture = log_lines()
    assert await _emit(kind, Claims())
    _flush()
    lines = [line for line in capture.lines() if line["message"] == f"alert.{kind}"]
    assert len(lines) == 1 and lines[0]["severity"] == "WARNING"
    for name, value in FIELDS[kind].items():
        assert lines[0][name] == str(value)
    [event] = _alerts(sentry.events())
    assert event["message"] == f"alert.{kind}" and event["level"] == "warning"
    assert event["tags"]["alert_kind"] == kind


async def test_alert_unknown_field_refused(sentry: Any) -> None:
    with pytest.raises(TypeError, match="unknown alert field"):
        await _emit("queue_latency", Claims(), fields={"listing_title": "x"})


@pytest.mark.parametrize("bad", [{"a": 1}, "x" * 129, ["l"], True, 1.5])
async def test_alert_text_value_refused(sentry: Any, bad: Any) -> None:
    claims = Claims()
    with pytest.raises(TypeError):
        await _emit("queue_latency", claims, fields={"queue": bad})
    assert not claims.seen  # refused before the dedupe claim


async def test_alert_suppressed_emits_nothing(sentry: Any, log_lines: Any) -> None:
    claims = Claims()
    slack = FakeSlack()
    capture = log_lines(log_level="DEBUG")
    assert await _emit("queue_latency", claims, slack=slack)
    assert not await _emit("queue_latency", claims, slack=slack)
    _flush()
    messages = [line["message"] for line in capture.lines()]
    assert messages.count("alert.queue_latency") == 1
    assert messages.count("alert.suppressed") == 1
    assert len(_alerts(sentry.events())) == 1 and len(slack.posts) == 1


async def test_alert_fingerprint_kind_key(sentry: Any) -> None:
    from app.core.alerts import fingerprint

    assert fingerprint("queue_latency", "default", {"queue": "default"}) == [
        "alert",
        "queue_latency",
        "default",
    ]
    await _emit("queue_latency", Claims(), key="default")
    _flush()
    [event] = _alerts(sentry.events())
    assert event["fingerprint"] == ["alert", "queue_latency", "default"]


async def test_alert_dead_letter_fingerprint_no_job_id(sentry: Any) -> None:
    claims = Claims()
    for _ in range(2):
        job = uuid.uuid4()
        await _emit("job_dead_lettered", claims, key=str(job), fields={"job_id": job})
    _flush()
    events = _alerts(sentry.events())
    assert len(events) == 2
    prints = {tuple(e["fingerprint"]) for e in events}
    assert prints == {("alert", "job_dead_lettered", "bulk_import", "JOB_TIMEOUT")}
    for event in events:
        assert not any(str(event["tags"]["job_id"]) in part for part in event["fingerprint"])


async def test_alert_burst_capped(sentry: Any, log_lines: Any) -> None:
    from app.core.alerts import dead_letter_cap

    claims = Claims()
    capture = log_lines(log_level="DEBUG")
    sent = 0
    for _ in range(25):
        job = uuid.uuid4()
        sent += await _emit(
            "job_dead_lettered",
            claims,
            key=str(job),
            fields={"job_id": job},
            cap=dead_letter_cap("bulk_import"),
            cap_claim=claims.cap,
        )
    _flush()
    assert sent == 20
    assert len(_alerts(sentry.events())) == 20
    lines = capture.lines()
    assert sum(1 for line in lines if line["message"] == "alert.job_dead_lettered") == 20
    capped = [line for line in lines if line["message"] == "alert.suppressed"]
    assert len(capped) == 5 and all(line["reason"] == "cap" for line in capped)


async def test_alert_cap_per_job_kind(sentry: Any) -> None:
    from app.core.alerts import dead_letter_cap

    claims = Claims()
    sent: Counter[str] = Counter()
    for job_kind in ("bulk_import", "brand_embed"):
        for _ in range(22):
            job = uuid.uuid4()
            sent[job_kind] += await _emit(
                "job_dead_lettered",
                claims,
                key=str(job),
                fields={"job_id": job, "job_kind": job_kind},
                cap=dead_letter_cap(job_kind),
                cap_claim=claims.cap,
            )
    assert sent == {"bulk_import": 20, "brand_embed": 20}


async def test_alert_no_double_sentry_event(sentry: Any, log_lines: Any) -> None:
    """The WARNING log line stays below the logging integration's event level."""
    log_lines()
    await _emit("llm_budget", Claims())
    _flush()
    assert len(sentry.events()) == 1


async def test_alert_slack_only_for_routed_kinds(sentry: Any) -> None:
    slack = FakeSlack()
    for kind in sorted(FIELDS):
        await _emit(kind, Claims(), slack=slack)
    assert sorted(kind for kind, _ in slack.posts) == [
        "llm_budget",
        "queue_latency",
        "token_refresh_failed",
    ]


async def test_alert_sink_failure_never_raises(sentry: Any, log_lines: Any) -> None:
    capture = log_lines()
    assert await _emit("queue_latency", Claims(), slack=FakeSlack(fail=True))
    assert "alert.slack_failed" in [line["message"] for line in capture.lines()]


def test_floor_window() -> None:
    from app.core.alerts import day_window, floor_window

    assert floor_window(NOW, timedelta(minutes=30)) == datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    assert floor_window(NOW, timedelta(minutes=15)) == datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    assert day_window(NOW) == datetime(2026, 10, 9, tzinfo=UTC)
    with pytest.raises(ValueError, match="aware"):
        floor_window(datetime(2026, 10, 9), timedelta(minutes=5))
