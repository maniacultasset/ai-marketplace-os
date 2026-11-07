"""Generation failure accounting: a transient model or embedding failure is a retryable
`MODEL_UNAVAILABLE`, and a run that never reached a model stage gives its reservation back."""

from __future__ import annotations

import dataclasses
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

LOCAL_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os"
NOW = datetime(2026, 11, 6, tzinfo=UTC)


def _deps() -> Any:
    from app.core.config import Settings
    from app.services.pipeline_deps import BackendPipelineDeps, _ModelConfig
    from app.services.rules_loader import LoadedRules
    from mkos_schemas.enums import Marketplace
    from mkos_schemas.rules import RuleSet

    cfg = Settings(_env_file=None, app_env="local", database_url=LOCAL_URL)
    ruleset = RuleSet(marketplace="amazon", category="kurta", loaded_at=NOW)
    return BackendPipelineDeps(
        tenant_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        user_id=None,
        marketplace=Marketplace.AMAZON,
        rules=LoadedRules(ruleset=ruleset, template=None),
        product=None,
        image_reports={},
        embedder=None,
        models=_ModelConfig.from_settings(cfg),
    )


@pytest.fixture
def no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """`emit_node_event("started")` writes `job_runs.node`; here the write is a no-op."""
    from app.services import pipeline_deps

    class _Jobs:
        async def set_node(self, *_: Any, **__: Any) -> bool:
            return True

    class _Uow:
        job_runs = _Jobs()

    @asynccontextmanager
    async def fake_uow(*_: Any, **__: Any) -> Any:
        yield _Uow()

    monkeypatch.setattr(pipeline_deps, "tenant_uow", fake_uow)


async def _started(deps: Any, *stages: str) -> None:
    from mkos_schemas.enums import PipelineStage

    for stage in stages:
        await deps.emit_node_event(PipelineStage(stage), "started")
        await deps.emit_node_event(PipelineStage(stage), "ok")


# ---- the model-stage signal on the deps ----


def test_no_stage_reported_counts_as_model_stage_reached() -> None:
    assert _deps().model_stage_reached is True


@pytest.mark.usefixtures("no_db")
async def test_context_and_retrieval_only_is_not_a_model_stage() -> None:
    deps = _deps()
    await _started(deps, "load_context", "retrieve_brand")
    assert deps.model_stage_reached is False


@pytest.mark.usefixtures("no_db")
@pytest.mark.parametrize(
    "stage", ["content_agent", "keyword_agent", "attribute_agent", "image_agent", "correct"]
)
async def test_a_writer_stage_is_a_model_stage(stage: str) -> None:
    deps = _deps()
    await _started(deps, "load_context", "retrieve_brand", stage)
    assert deps.model_stage_reached is True


# ---- transient port failures ----


@pytest.mark.parametrize("code", ["MODEL_UNAVAILABLE", "TIMEOUT", "RATE_LIMITED"])
def test_transient_port_error(code: str) -> None:
    from app.services.pipeline_deps import SeamPortError
    from app.workers.generation import is_transient_failure
    from mkos_schemas.codes import ErrorCode

    err = SeamPortError(ErrorCode(code), "embedding", retryable=True)
    assert is_transient_failure(err) is True


def test_transient_exception_group_needs_every_leaf_transient() -> None:
    from app.services.pipeline_deps import SeamPortError
    from app.workers.generation import is_transient_failure
    from mkos_schemas.codes import ErrorCode

    down = SeamPortError(ErrorCode.MODEL_UNAVAILABLE, "embedding", retryable=True)
    slow = SeamPortError(ErrorCode.TIMEOUT, "embedding", retryable=True)
    assert is_transient_failure(ExceptionGroup("x", [down, slow])) is True
    assert is_transient_failure(ExceptionGroup("x", [down, RuntimeError("boom")])) is False


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("boom"),
        ValueError("bad"),
        "foreign_tenant",
        "bad_query",
    ],
)
def test_not_transient(exc: Any) -> None:
    from app.services.pipeline_deps import SeamPortError
    from app.workers.generation import is_transient_failure
    from mkos_schemas.codes import ErrorCode

    if exc == "foreign_tenant":
        exc = SeamPortError(ErrorCode.INTERNAL_ERROR, "tenant_id")
    elif exc == "bad_query":
        exc = SeamPortError(ErrorCode.VALIDATION_FAILED, "query")
    assert is_transient_failure(exc) is False


def test_a_permanent_port_error_with_a_transient_code_is_not_transient() -> None:
    """A missing key or a refused embeddings call carries MODEL_UNAVAILABLE but will repeat."""
    from app.services.pipeline_deps import SeamPortError
    from app.workers.generation import is_transient_failure
    from mkos_schemas.codes import ErrorCode

    assert is_transient_failure(SeamPortError(ErrorCode.MODEL_UNAVAILABLE, "content")) is False


async def test_missing_generation_key_outside_local_is_not_retryable() -> None:
    from app.core.config import Settings
    from app.services.pipeline_deps import SeamPortError, _ModelConfig
    from app.workers.generation import is_transient_failure

    deps = _deps()
    models = _ModelConfig.from_settings(
        Settings(_env_file=None, app_env="local", database_url=LOCAL_URL)
    )
    deps._models = dataclasses.replace(models, app_env="staging", generation_key=None)
    with pytest.raises(SeamPortError) as caught:
        await deps.get_model_credentials("content")
    assert caught.value.retryable is False
    assert is_transient_failure(caught.value) is False


async def test_no_embedder_is_not_retryable() -> None:
    from app.services.pipeline_deps import SeamPortError
    from app.workers.generation import is_transient_failure

    deps = _deps()
    with pytest.raises(SeamPortError) as caught:
        await deps.retrieve_brand_chunks(deps._tenant_id, "kurta", 8)
    assert caught.value.retryable is False
    assert is_transient_failure(caught.value) is False


@pytest.mark.parametrize(("retryable", "transient"), [(False, False), (True, True)])
async def test_embedding_failure_keeps_its_retryable_flag(
    monkeypatch: pytest.MonkeyPatch, retryable: bool, transient: bool
) -> None:
    """A 401 from the embeddings API is not retried; a 429 is."""
    from app.services import pipeline_deps
    from app.services.embeddings import EmbeddingFailed
    from app.services.pipeline_deps import SeamPortError
    from app.workers.generation import is_transient_failure
    from mkos_schemas.codes import ErrorCode

    async def refused(*_: Any, **__: Any) -> Any:
        raise EmbeddingFailed(ErrorCode.MODEL_UNAVAILABLE, retryable=retryable, status=401)

    monkeypatch.setattr(pipeline_deps, "embed_query", refused)
    deps = _deps()
    deps._embedder = object()
    with pytest.raises(SeamPortError) as caught:
        await deps.retrieve_brand_chunks(deps._tenant_id, "kurta", 8)
    assert caught.value.retryable is retryable
    assert is_transient_failure(caught.value) is transient


# ---- transient agent runs of a no-version result ----


def _record(status: str, code: str | None, agent: str = "content") -> Any:
    from mkos_schemas.agent import AgentRunRecord
    from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
    from mkos_schemas.enums import ErrorSource, RetryClass
    from mkos_schemas.errors import ErrorItem, ErrorReport

    report = None
    if code is not None:
        error_code = ErrorCode(code)
        meta = ERROR_CODE_META[error_code]
        retryable = meta.retry_class in (RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT)
        report = ErrorReport(
            status="failed",
            error_code=error_code,
            retryable=retryable,
            errors=[
                ErrorItem(
                    field="listing",
                    error_code=error_code,
                    message="the call failed",
                    source=ErrorSource.AI_MODEL,
                    retryable=retryable,
                    retry_class=meta.retry_class,
                )
            ],
        )
    return AgentRunRecord(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        request_id="req-1",
        agent_name=agent,
        trigger="generate",
        status=status,
        error_report=report,
        started_at=NOW,
        created_at=NOW,
    )


def _block(agent: str, status: str) -> Any:
    from mkos_schemas.jobs import AgentBlockStatus

    return AgentBlockStatus(agent=agent, status=status)


def test_runs_transient_when_every_agent_that_ended_failed_failed_transiently() -> None:
    from app.workers.generation import runs_failed_transiently

    runs = [
        _record("completed", None, "content"),
        _record("failed", "RATE_LIMITED", "keyword"),
        _record("failed", "MODEL_UNAVAILABLE", "keyword"),
        _record("timeout", None, "attribute"),
        _record("timeout", None, "attribute"),
    ]
    blocks = [_block("content", "ok"), _block("keyword", "failed"), _block("attribute", "failed")]
    assert runs_failed_transiently(runs, blocks) is True


@pytest.mark.parametrize(
    ("runs", "blocks"),
    [
        ([], []),
        ([_record("completed", None)], []),
        ([_record("failed", "RATE_LIMITED"), _record("failed", "GENERATION_FAILED")], []),
        ([_record("failed", None)], []),
        ([_record("failed", "SCHEMA_INVALID")], []),
        # keyword's 429 recovered on the retry; the run then failed for another reason
        (
            [
                _record("failed", "RATE_LIMITED", "keyword"),
                _record("completed", None, "keyword"),
                _record("completed", None, "content"),
                _record("completed", None, "attribute"),
            ],
            [_block("content", "ok"), _block("keyword", "ok"), _block("attribute", "ok")],
        ),
        # a sibling's recovered 429, then content returned an empty title (completed, failed block)
        (
            [
                _record("failed", "RATE_LIMITED", "keyword"),
                _record("completed", None, "keyword"),
                _record("completed", None, "content"),
            ],
            [_block("content", "failed"), _block("keyword", "ok")],
        ),
        # a transient keyword failure next to a deterministic content failure
        (
            [
                _record("failed", "RATE_LIMITED", "keyword"),
                _record("failed", "RATE_LIMITED", "keyword"),
                _record("completed", None, "content"),
            ],
            [_block("content", "failed"), _block("keyword", "failed")],
        ),
    ],
)
def test_runs_not_transient(runs: list[Any], blocks: list[Any]) -> None:
    from app.workers.generation import runs_failed_transiently

    assert runs_failed_transiently(runs, blocks) is False


# ---- the report ----


def test_job_report_retryable_follows_the_code() -> None:
    from app.workers.generation import job_report
    from app.workers.registry import JobContext
    from mkos_schemas.codes import ErrorCode
    from mkos_schemas.enums import JobKind, RetryClass

    ctx = JobContext(uuid.uuid4(), uuid.uuid4(), JobKind.LISTING_GENERATE, "req-1")
    down = job_report(ErrorCode.MODEL_UNAVAILABLE, "try again", ctx=ctx)
    assert down.retryable is True
    assert [(e.retryable, e.retry_class) for e in down.errors] == [(True, RetryClass.AUTO_RETRY)]
    internal = job_report(ErrorCode.INTERNAL_ERROR, "generation failed", ctx=ctx)
    assert internal.retryable is False and internal.errors[0].retryable is False


# ---- SI3 T11: a failed photo call never fails generation ----


def test_a_failed_photo_record_is_not_a_transient_generation_failure() -> None:
    from app.workers.generation import runs_failed_transiently

    photo = _record("timeout", None, "image").model_copy(
        update={"trigger": "vision", "prompt_version": "photo_facts/v1"}
    )
    runs = [
        _record("completed", None, "content"),
        _record("completed", None, "keyword"),
        _record("completed", None, "attribute"),
        _record("completed", None, "image"),
        photo,
    ]
    assert runs_failed_transiently(runs, []) is False
    assert runs_failed_transiently([*runs[:3], photo], []) is False
    failed_photo = photo.model_copy(update={"status": "failed"})
    assert runs_failed_transiently([_record("completed", None, "content"), failed_photo]) is False
