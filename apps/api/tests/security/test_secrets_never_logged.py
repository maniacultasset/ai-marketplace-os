"""S26 T11 (AC 3; invariant 6): secrets never leak.

One condensed flow in process: a login whose password is a canary, a marketplace connect over the
fake OAuth (every token it issues is `CANARYzq9-<hex>`), a CSV import, token refreshes (ok, then
`invalid_grant`), a refresh-token rotation and a logout. Then no sink holds the password, a
marketplace token, the issued access token or the issued refresh token: log lines, Sentry
events, OTel spans, queue payloads (`procrastinate_jobs.args`, `job_runs` JSON), ErrorReports,
`agent_runs`, `audit_log`, `listing_submissions`, human tasks and every API answer except the two
that hand the caller its own tokens. The stored `token_enc` holds no canary byte, only
`app.adapters.credentials` unwraps a key, and every GenerationContext the stub is pushed passes
the seam's secret walk.
"""

from __future__ import annotations

import ast
import json
import secrets
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.integration.marketplace_support import (
    MktWorld,
    callback,
    confirm,
    connect,
    consent,
    mkt,
    pending_of,
)

__all__ = ["mkt"]
APP = Path(__file__).resolve().parents[2] / "app"
TOKEN_CANARY = "CANARYzq9"
DSN = "https://public@o0.ingest.example.invalid/0"
SKIP_JWT = ("/auth/login", "/auth/refresh")  # the answers that hand the caller its own tokens


def _db_corpus(conn: Any) -> dict[str, str]:
    def rows(query: str) -> str:
        return "\n".join(str(r[0]) for r in conn.execute(query).fetchall())

    return {
        "procrastinate_jobs.args": rows("SELECT args::text FROM procrastinate_jobs"),
        "job_runs": rows(
            "SELECT concat_ws('|', payload_json::text, result_json::text, "
            "error_report_json::text, error_code) FROM job_runs"
        ),
        "audit_log": rows(
            "SELECT concat_ws('|', action, before::text, after::text, reason, actor_email) "
            "FROM audit_log"
        ),
        "agent_runs": rows("SELECT row_to_json(a)::text FROM agent_runs a"),
        "listing_submissions": rows("SELECT row_to_json(s)::text FROM listing_submissions s"),
        "human_tasks": rows("SELECT row_to_json(t)::text FROM human_tasks t"),
        "accounts": rows(
            "SELECT concat_ws('|', seller_id, last_refresh_error_code, "
            "encode(token_enc, 'escape')) FROM marketplace_accounts"
        ),
        "imports": rows("SELECT row_to_json(i)::text FROM imports i"),
    }


@pytest.fixture
async def flow(
    mkt: MktWorld,
    monkeypatch: pytest.MonkeyPatch,
    log_lines: Any,
    sentry_events: Any,
) -> dict[str, Any]:
    from app.core.config import get_settings
    from app.core.passwords import hash_password
    from app.main import create_app
    from app.workers.token_refresh import refresh_account
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from tests.integration.imports_support import MAPPED, csv_bytes, row, seed_templates

    monkeypatch.setenv("SENTRY_DSN", DSN)
    get_settings.cache_clear()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    app = create_app()
    capture = log_lines()  # after create_app(): it configures logging
    app.tracer = provider.get_tracer("s26-secrets")  # type: ignore[attr-defined]
    password = f"CANARYpw-{secrets.token_hex(8)}"
    mkt.conn.execute(
        "UPDATE users SET hashed_password = %s WHERE id = %s",
        [hash_password(password), mkt.user("owner@acme.test")],
    )
    seed_templates(mkt.conn)
    answers: list[tuple[str, str]] = []
    transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as api:

        def keep(path: str, response: httpx.Response) -> httpx.Response:
            answers.append((path, response.text + json.dumps(dict(response.headers))))
            return response

        login = keep(
            "/auth/login",
            await api.post(
                "/api/v1/auth/login", json={"email": "owner@acme.test", "password": password}
            ),
        )
        assert login.status_code == 200, login.text
        pair = login.json()
        owner = {"Authorization": f"Bearer {pair['access_token']}"}
        authorize_url = await connect(api, mkt, "owner@acme.test", "amazon")
        query = await consent(mkt, authorize_url, "A1S26SECRET")
        handle = pending_of(keep("callback", await callback(api, "amazon", query)))
        confirmed = keep("confirm", await confirm(api, mkt, "owner@acme.test", handle))
        assert confirmed.status_code == 200, confirmed.text
        account_id = uuid.UUID(confirmed.json()["id"])
        upload = keep(
            "/products/bulk",
            await api.post(
                "/api/v1/products/bulk",
                headers={**owner, "Idempotency-Key": str(uuid.uuid4())},
                files={"file": ("p.csv", csv_bytes([row(1, category=MAPPED[0])]), "text/csv")},
            ),
        )
        assert upload.status_code == 202, upload.text
        keep("job", await api.get(upload.json()["poll_url"], headers=owner))
        for outcome in ("ok", "invalid_grant"):
            mkt.conn.execute(
                "UPDATE marketplace_accounts SET expires_at = now() + interval '1 minute'"
            )
            mkt.control.next_refresh = outcome
            await refresh_account(mkt.tenant("acme"), account_id, force=True)
        keep("accounts", await api.get("/api/v1/marketplaces/accounts", headers=owner))
        keep("tasks", await api.get("/api/v1/tasks", headers=owner))
        rotated = keep(
            "/auth/refresh",
            await api.post("/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}),
        )
        assert rotated.status_code == 200, rotated.text
        new = rotated.json()
        keep(
            "logout",
            await api.post("/api/v1/auth/logout", json={"refresh_token": new["refresh_token"]}),
        )
        keep("me-after", await api.get("/api/v1/auth/me", headers=owner))
    import sentry_sdk

    sentry_sdk.get_client().flush(timeout=2)
    marketplace_tokens = [
        r["refresh_token"] for r in mkt.control.token_requests if r.get("refresh_token")
    ]
    return {
        "mkt": mkt,
        "password": password,
        "secrets": [
            password,
            TOKEN_CANARY,
            pair["access_token"],
            pair["refresh_token"],
            new["access_token"],
            new["refresh_token"],
            *marketplace_tokens,
        ],
        "logs": capture.text(),
        "sentry": sentry_events.serialized(),
        "spans": "\n".join(s.to_json() for s in exporter.get_finished_spans()),
        "answers": answers,
    }


def _clean(name: str, text: str, values: list[str]) -> None:
    for value in values:
        assert value not in text, (name, value[:12])


def test_secrets_never_logged_in_logs(flow: dict[str, Any]) -> None:
    assert flow["logs"], "the flow logged nothing"
    _clean("logs", flow["logs"], flow["secrets"])


def test_secrets_never_logged_in_sentry_events(flow: dict[str, Any]) -> None:
    _clean("sentry", flow["sentry"], flow["secrets"])


def test_secrets_never_logged_in_spans(flow: dict[str, Any]) -> None:
    assert flow["spans"], "no span was recorded"
    _clean("spans", flow["spans"], flow["secrets"])


@pytest.mark.parametrize(
    "sink",
    [
        "procrastinate_jobs.args",
        "job_runs",
        "audit_log",
        "agent_runs",
        "listing_submissions",
        "human_tasks",
        "accounts",
        "imports",
    ],
)
def test_secrets_never_logged_in_db_sinks(flow: dict[str, Any], sink: str) -> None:
    """Job payloads, ErrorReports, agent runs, audit rows, submissions, tasks and accounts."""
    corpus = _db_corpus(flow["mkt"].conn)
    _clean(sink, corpus[sink], flow["secrets"])


def test_secrets_never_logged_in_api_responses(flow: dict[str, Any]) -> None:
    for path, text in flow["answers"]:
        values = [flow["password"], TOKEN_CANARY]
        if path not in SKIP_JWT:
            values = flow["secrets"]
        _clean(f"answer {path}", text, values)


def test_token_enc_has_no_canary_bytes(flow: dict[str, Any]) -> None:
    rows = (
        flow["mkt"]
        .conn.execute("SELECT token_enc FROM marketplace_accounts WHERE token_enc IS NOT NULL")
        .fetchall()
    )
    assert rows
    for (blob,) in rows:
        assert TOKEN_CANARY.encode() not in bytes(blob)
        assert b"refresh_token" not in bytes(blob)


def test_token_enc_decrypts_only_via_adapter_path() -> None:
    """Only `app.adapters.credentials` calls a key unwrapper's `unwrap` for seller tokens, and
    `unseal` is called only by the two worker paths that need a live token (the adapter session
    and the refresh job). DPP-1 (ADR-0021): `app.adapters.mfa_secrets` is the one other unwrapper,
    for the MFA data-key label only; the import-linter contract
    `mfa-secret-unseal-only-in-mfa-service` keeps it behind `app.services.mfa`, and it never
    imports the token path."""
    unwrap_callers: set[str] = set()
    unseal_callers: set[str] = set()
    for path in sorted(APP.rglob("*.py")):
        rel = str(path.relative_to(APP))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "unwrap":
                unwrap_callers.add(rel)
            if name == "unseal":
                unseal_callers.add(rel)
    assert unwrap_callers == {"adapters/credentials.py", "adapters/mfa_secrets.py"}, unwrap_callers
    mfa_tree = ast.parse((APP / "adapters" / "mfa_secrets.py").read_text(encoding="utf-8"))
    mfa_imports = {
        alias.name for n in ast.walk(mfa_tree) if isinstance(n, ast.Import) for alias in n.names
    } | {n.module or "" for n in ast.walk(mfa_tree) if isinstance(n, ast.ImportFrom)}
    assert not any(m.startswith("app.adapters.credentials") for m in mfa_imports), mfa_imports
    assert unseal_callers <= {"workers/adapters.py", "workers/token_refresh.py"}, unseal_callers


async def test_every_pushed_generation_context_passes_seam_walk(
    auth_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mkos_pipeline
    from app.workers import generation as generation_worker
    from mkos_schemas.base import find_seam_secret_key, walk_seam_dicts_and_text
    from tests.integration.generation_support import build_gen_world, make_generating
    from tests.integration.review_support import new_product
    from tests.integration.review_support import run_job as run_generation

    seen: list[Any] = []
    original = mkos_pipeline.run_listing_pipeline

    async def spy(ctx: Any, deps: Any) -> Any:
        seen.append(ctx)
        return await original(ctx, deps)

    monkeypatch.setattr(generation_worker, "run_listing_pipeline", spy)
    world = await build_gen_world(auth_db, monkeypatch)
    for scenario in (None, "stub:blocked", "stub:warnings"):
        sku = await new_product(world)
        accepted = await make_generating(world, sku=sku, instructions=scenario)
        await run_generation(accepted.job_id)
    assert len(seen) == 3
    for ctx in seen:
        walk_seam_dicts_and_text(ctx)  # raises on a secret-shaped key at any depth
        assert find_seam_secret_key(json.loads(ctx.model_dump_json())) is None
        assert TOKEN_CANARY not in ctx.model_dump_json()
