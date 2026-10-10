"""S26 T10 (AC 2, OWASP API9 + API10): improper inventory and unsafe consumption of APIs.

API9: the mounted routes, the served OpenAPI and the registry are the same set, and the
committed `packages/schemas/openapi.json` is exactly what the app serves.
API10: a hostile marketplace reply (malformed JSON, an extra field, an oversize or HTML body, a
redirect) ends the publish job with one ErrorReport, never a 500 or a traceback, and no redirect
is followed; a hostile OAuth refresh reply ends in `AUTH_EXPIRED` or `MARKETPLACE_UNAVAILABLE`
and its bytes (a canary) reach no ErrorReport, log line, Sentry event or account row; a pipeline
result failing strict validation is refused with no version stored, and an extra key in a
pipeline result is dropped, never persisted.
"""

from __future__ import annotations

import json
import secrets
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.api.v1.surface import OPS
from fastapi.routing import iter_route_contexts
from tests.integration.failure_support import AmzWorld, amz
from tests.integration.generation_support import GenWorld, gen_world, make_generating
from tests.integration.marketplace_support import MktWorld, connected_account, mapi, mkt
from tests.integration.publish_support import approved, publish_one, run_job

__all__ = ["amz", "gen_world", "mapi", "mkt"]
REPO = Path(__file__).resolve().parents[4]
# Unversioned app routes (ENV.md:13): the probes, and the docs FastAPI serves outside production.
UNVERSIONED = frozenset({"/healthz", "/readyz", "/openapi.json", "/docs", "/docs/oauth2-redirect"})
METHODS = frozenset({"GET", "POST", "PATCH", "DELETE", "PUT"})


def _app() -> Any:
    from app.main import create_app

    return create_app()


def test_api9_mounted_routes_equal_openapi_equal_registry(auth_db: Any) -> None:
    app = _app()
    mounted: set[tuple[str, str]] = set()
    for route in iter_route_contexts(list(app.routes)):
        path = getattr(route, "path", "")
        if not path.startswith("/api/"):
            assert path in UNVERSIONED or path.startswith("/internal/admin"), path
            continue
        for method in getattr(route, "methods", None) or ():
            if method in METHODS:
                mounted.add((method, path))
    spec = {
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        for method in item
        if path.startswith("/api/")
    }
    registry = {(op.method, f"/api/v1{op.path}") for op in OPS.values()}
    assert mounted == spec == registry


def test_api9_committed_openapi_matches_app(auth_db: Any) -> None:
    from app.openapi_export import build, render

    committed = (REPO / "packages/schemas/jsonschema/openapi.json").read_text(encoding="utf-8")
    assert committed == render(build())


def _no_traceback(text: str) -> None:
    for leak in ("Traceback", 'File "', "pydantic_core", "JSONDecodeError"):
        assert leak not in text, leak


@pytest.mark.parametrize("shape", ["malformed_json", "extra_field", "oversize", "html", "redirect"])
async def test_api10_marketplace_reply_errorreport(
    api: httpx.AsyncClient,
    amz: AmzWorld,
    socket_spy: list[tuple[str, int]],
    shape: str,
) -> None:
    from mkos_schemas.errors import ErrorReport

    made = await approved(amz.pub)
    await amz.control(spapi_body=shape)
    item = await publish_one(api, amz.pub, made)
    try:
        await run_job(uuid.UUID(item["job_id"]))
    except Exception as exc:  # RetryLater: the job goes back to queued
        assert type(exc).__name__ == "RetryLater", repr(exc)
    job = amz.pub.job(item["job_id"])
    (sub,) = amz.pub.submissions(made.listing_id)
    state = (job["status"], sub["outcome"], sub["error_code"])
    if shape == "extra_field":
        # a valid reply with one more key: the client's reply model drops it
        assert state[:2] == ("succeeded", "accepted"), state
        assert "unexpected" not in json.dumps(sub, default=str)
    elif sub["outcome"] in (None, "pending"):
        # an ambiguous answer (the write may have landed): the submission stays pending and
        # the job goes back to the queue to read before it retries (S20/S21), never `running`
        assert job["status"] == "queued", state
        assert sub["responded_at"] is None, state
    else:
        report = sub["error_report_json"] or job["error_report_json"]
        assert report is not None, state
        ErrorReport.model_validate(report)
        _no_traceback(json.dumps(report))
    _no_traceback(json.dumps([job, sub], default=str))
    status = await api.get(
        f"/api/v1/jobs/{item['job_id']}", headers=amz.world.headers("operator@acme.test")
    )
    assert status.status_code == 200, status.text
    _no_traceback(status.text)
    if shape == "redirect":
        writes = [c for c in amz.state.calls if c["method"] in ("PUT", "PATCH")]
        assert len(writes) == 1, amz.state.calls  # the redirect target was never asked
        assert all("REDIRECTED" not in c["path"] for c in amz.state.calls)
    from app.core.config import get_settings

    store = httpx.URL(get_settings().s3_endpoint_url).port  # the object store (MinIO) is allowed
    hostile = [t for t in socket_spy if t[0] == "169.254.169.254" or (t[1] and t[1] != store)]
    assert hostile == [], hostile


@pytest.mark.parametrize("outcome", ["malformed", "oversize", "extra_field"])
async def test_api10_oauth_reply_errorreport(
    mkt: MktWorld,
    mapi: httpx.AsyncClient,
    log_lines: Any,
    sentry_events: Any,
    outcome: str,
) -> None:
    from app.workers.token_refresh import refresh_account

    capture = log_lines()
    account = await connected_account(mapi, mkt)
    account_id = uuid.UUID(account["id"])
    canary = f"CANARYzq9-{secrets.token_hex(8)}"
    mkt.conn.execute(
        "UPDATE marketplace_accounts SET expires_at = now() + interval '1 minute' WHERE id = %s",
        [account_id],
    )
    mkt.control.next_refresh = outcome
    mkt.control.refresh_canary = canary
    result = await refresh_account(mkt.tenant("acme"), account_id, force=True)
    row = mkt.conn.execute(
        "SELECT status, last_refresh_error_code, encode(token_enc, 'escape') "
        "FROM marketplace_accounts WHERE id = %s",
        [account_id],
    ).fetchone()
    assert row is not None
    if outcome == "extra_field":
        # a valid token reply with one more key: refreshed, the key dropped
        assert row[0] == "active", (result, row)
    else:
        # S15: an unreadable token reply is a transient failure (the account stays active and
        # is retried); a refused grant is AUTH_EXPIRED
        assert row[1] in (
            "AUTH_EXPIRED",
            "MARKETPLACE_UNAVAILABLE",
            "MARKETPLACE_UNKNOWN_ERROR",
        ), (result, row)
    accounts = await mapi.get(
        "/api/v1/marketplaces/accounts",
        headers={"Authorization": f"Bearer {_owner_token(mkt)}"},
    )
    assert accounts.status_code == 200
    reports = mkt.conn.execute(
        "SELECT coalesce(string_agg(coalesce(evidence::text, '') || reason, '|'), '') "
        "FROM human_tasks"
    ).fetchone()
    corpus = {
        "logs": capture.text(),
        "sentry": sentry_events.serialized(),
        "account": str(row[2]) + str(row[1]),
        "response": accounts.text,
        "tasks": str(reports[0] if reports else ""),
    }
    for name, text in corpus.items():
        assert canary not in text, name


def _owner_token(mkt: MktWorld) -> str:
    from tests.integration.auth_support import tokens_for

    return tokens_for(mkt.world, "owner@acme.test", "acme")


async def test_api10_pipeline_invalid_result_rejected(gen_world: GenWorld) -> None:
    from tests.integration.review_support import run_job as run_generation

    accepted = await make_generating(gen_world, instructions="stub:malformed")
    await run_generation(accepted.job_id)
    job = gen_world.conn.execute(
        "SELECT status, error_code, error_report_json FROM job_runs WHERE id = %s",
        [accepted.job_id],
    ).fetchone()
    assert job is not None and (job[0], job[1]) == ("failed", "INTERNAL_ERROR"), job
    assert {e["source"] for e in job[2]["errors"]} == {"system"}
    versions = gen_world.scalar(
        "SELECT count(*) FROM listing_versions WHERE listing_id = %s", [accepted.listing_id]
    )
    assert versions == 0
    row = gen_world.listing(accepted.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "generation_failed")


async def test_api10_pipeline_extra_key_dropped_not_persisted(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipeline hands back a result whose JSON carries keys the contract does not name; the
    backend's strict re-validation (`model_validate_json`) drops them before anything is stored."""
    import mkos_pipeline
    from app.workers import generation as generation_worker
    from tests.integration.review_support import run_job as run_generation

    original = mkos_pipeline.run_listing_pipeline

    class Smuggled:
        def __init__(self, result: Any) -> None:
            self._result = result

        def __getattr__(self, name: str) -> Any:
            return getattr(self._result, name)

        def model_dump_json(self, **kwargs: Any) -> str:
            body = json.loads(self._result.model_dump_json(**kwargs))
            body["unexpected"] = "s26-smuggled"
            if isinstance(body.get("listing"), dict):
                body["listing"]["unexpected"] = "s26-smuggled"
                body["listing"]["content"]["unexpected"] = "s26-smuggled"
            return json.dumps(body)

    async def smuggle(ctx: Any, deps: Any) -> Any:
        return Smuggled(await original(ctx, deps))

    monkeypatch.setattr(generation_worker, "run_listing_pipeline", smuggle)
    accepted = await make_generating(gen_world)
    await run_generation(accepted.job_id)
    stored = gen_world.conn.execute(
        "SELECT content_json::text FROM listing_versions WHERE listing_id = %s",
        [accepted.listing_id],
    ).fetchall()
    for (text,) in stored:
        assert "s26-smuggled" not in text and "unexpected" not in text
    detail = await api.get(
        f"/api/v1/listings/{accepted.listing_id}",
        headers=gen_world.headers("operator@acme.test"),
    )
    assert "s26-smuggled" not in detail.text
