"""The owner's one-off Amazon dry run (`app.workers.amazon_preview`) on the SI11 preview world, with
the in-process adapter: the listing and account guards, the staff check, the audit row, a GET of
each SKU before and after, and no write of any kind (no job, no issue row, no status move, no
mutating adapter call)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem

from .auth_support import audit_rows
from .conftest import LOCAL_HOSTS
from .preview_support import prev, preview_jobs, preview_rows
from .publish_support import PubWorld
from .review_support import REVIEWER, generated, run_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev"]
STAFF = REVIEWER


def _settings(**over: Any) -> Any:
    from app.core.config import get_settings

    return get_settings().model_copy(update={"amazon_preview_oneoff": True, **over})


def _deps(adapter: Any) -> Any:
    from app.workers.amazon_preview import PreviewDeps

    async def opener(tenant_id: uuid.UUID, account_id: uuid.UUID, transport: Any) -> Any:
        return adapter

    return PreviewDeps(opener=opener)


def _admin(pw: PubWorld, email: str = STAFF) -> None:
    pw.conn.execute("UPDATE users SET is_platform_admin = true WHERE email = %s", [email])


async def _validated(pw: PubWorld) -> Any:
    """A generated listing whose automatic preview job already ran (nothing live)."""
    made = await generated(pw.world)
    for job in preview_jobs(pw, made.listing_id):
        if job["status"] == "queued":
            await run_job(job["id"])
    return made


async def _oneoff(pw: PubWorld, made: Any, **kwargs: Any) -> Any:
    from app.workers.amazon_preview import run_oneoff

    return await run_oneoff(
        pw.world.tenant("acme"),
        made.listing_id,
        staff_email=kwargs.pop("staff_email", STAFF),
        precheck=kwargs.pop("precheck", True),  # the preview world turns the pre-check on
        deps=kwargs.pop("deps", _deps(pw.fake)),
        settings=kwargs.pop("settings", _settings()),
    )


def _jobs(pw: PubWorld) -> int:
    return int(pw.scalar("SELECT count(*) FROM job_runs"))


def _title_error() -> ErrorItem:
    return ErrorItem(
        field="content.title",
        error_code=ErrorCode.VALUE_NOT_ALLOWED,
        severity=Severity.ERROR,
        message="The title is longer than Amazon allows.",
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=False,
        raw_code="99301",
    )


async def test_one_off_previews_prints_and_writes_nothing(prev: PubWorld) -> None:
    _admin(prev)
    made = await _validated(prev)
    before_listing = prev.listing(made.listing_id)
    jobs, rows, calls = _jobs(prev), preview_rows(prev, made.listing_id), prev.fake.preview_calls
    prev.fake.preview_errors = [_title_error()]

    report = await _oneoff(prev, made)

    assert report.exit_code == 0 and report.stop == []
    assert report.verdict is not None and report.verdict.status == "issues"
    assert prev.fake.preview_calls == calls + 1
    assert prev.fake.calls == []  # no create, update or publish reached the adapter
    assert prev.fake.precheck_calls[-1] is True
    assert {(s.before, s.reply, s.after) for s in report.skus} == {("404", "INVALID", "404")}
    text = report.render()
    assert "verdict: issues" in text and '"attribute": "content.title"' in text
    assert '"amazon_code": "99301"' in text and "STOP" not in text
    # the verdict is printed, never written: no job, no issue row, no status or check change
    assert _jobs(prev) == jobs
    assert preview_rows(prev, made.listing_id) == rows
    after_listing = prev.listing(made.listing_id)
    for key in ("status", "status_reason", "remote_check_status", "approved_version_id"):
        assert after_listing[key] == before_listing[key], key
    (audit,) = audit_rows(prev.conn, "listing.preview_oneoff")
    assert audit[1] == "staff" and audit[3] == STAFF and audit[5] == made.listing_id
    assert audit[9] == "amazon_preview_oneoff"


async def test_refused_without_the_one_off_switch(prev: PubWorld) -> None:
    from app.workers.amazon_preview import Refused

    _admin(prev)
    made = await _validated(prev)
    calls = prev.fake.preview_calls
    with pytest.raises(Refused, match="AMAZON_PREVIEW_ONEOFF"):
        await _oneoff(prev, made, settings=_settings(amazon_preview_oneoff=False))
    with pytest.raises(Refused, match="AMAZON_CATALOG_PRECHECK_ENABLED"):
        await _oneoff(prev, made, precheck=False)
    assert prev.fake.preview_calls == calls
    assert audit_rows(prev.conn, "listing.preview_oneoff") == []


async def test_refused_for_a_staff_email_that_is_not_a_platform_admin(prev: PubWorld) -> None:
    from app.workers.amazon_preview import NotEligible

    made = await _validated(prev)
    with pytest.raises(NotEligible, match="platform admin"):
        await _oneoff(prev, made)
    with pytest.raises(NotEligible, match="platform admin"):
        await _oneoff(prev, made, staff_email="nobody@example.com")


async def test_refused_while_the_account_breaker_is_open(prev: PubWorld) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.remote_preview import open_breaker
    from app.workers.amazon_preview import NotEligible

    _admin(prev)
    made = await _validated(prev)
    async with tenant_uow(prev.world.tenant("acme")) as uow:
        await open_breaker(uow, prev.accounts["acme"])
    calls = prev.fake.preview_calls
    with pytest.raises(NotEligible, match="breaker"):
        await _oneoff(prev, made)
    assert prev.fake.preview_calls == calls


async def test_refused_for_an_inactive_account(prev: PubWorld) -> None:
    from app.workers.amazon_preview import NotEligible

    _admin(prev)
    made = await _validated(prev)
    prev.conn.execute(
        "UPDATE marketplace_accounts SET status = 'revoked', token_enc = NULL, "
        "kms_key_version = NULL, revoked_at = now() WHERE id = %s",
        [prev.accounts["acme"]],
    )
    with pytest.raises(NotEligible, match="not active"):
        await _oneoff(prev, made)


async def test_refused_for_a_listing_of_another_status(prev: PubWorld) -> None:
    from app.workers.amazon_preview import NotEligible

    _admin(prev)
    made = await generated(prev.world, scenario="stub:blocked")
    assert prev.listing(made.listing_id)["status_reason"] == "corrections_exhausted"
    with pytest.raises(NotEligible, match="status blocked/corrections_exhausted"):
        await _oneoff(prev, made)
    with pytest.raises(NotEligible, match="no such listing"):
        await _oneoff(prev, type(made)(uuid.uuid4(), made.version_id, made.job_id))


async def test_refused_while_a_sync_job_of_the_listing_is_live(prev: PubWorld) -> None:
    from app.workers.amazon_preview import NotEligible

    _admin(prev)
    made = await generated(prev.world)  # its automatic preview job is still queued
    assert [j["status"] for j in preview_jobs(prev, made.listing_id)] == ["queued"]
    with pytest.raises(NotEligible, match="live"):
        await _oneoff(prev, made)


class _Exists:
    """An adapter for which every SKU already exists on Amazon; a preview fails the test."""

    async def get_listing(self, seller_sku: str, *, listing_id: uuid.UUID | None = None) -> Any:
        return object()

    async def preview_remote(self, item: Any) -> Any:
        raise AssertionError("no preview may be sent for a SKU that exists")

    async def aclose(self) -> None:
        return None


async def test_stop_before_the_preview_when_a_sku_exists(prev: PubWorld) -> None:
    _admin(prev)
    made = await _validated(prev)
    report = await _oneoff(prev, made, deps=_deps(_Exists()))
    assert report.exit_code == 5 and report.verdict is None
    assert "already exist on Amazon; no preview sent" in report.render()


async def test_stop_and_breaker_when_the_reply_looks_applied(prev: PubWorld) -> None:
    _admin(prev)
    made = await _validated(prev)
    prev.fake.preview_status, prev.fake.preview_applied = "failed", True
    jobs = _jobs(prev)
    report = await _oneoff(prev, made)
    assert report.exit_code == 5 and "may have been applied" in report.render()
    tasks = prev.rows(
        "SELECT kind, status FROM human_tasks WHERE marketplace_account_id = %s "
        "AND target_path = 'account.preview_applied'",
        [prev.accounts["acme"]],
    )
    assert tasks == [("marketplace_error_review", "open")]
    assert _jobs(prev) == jobs
