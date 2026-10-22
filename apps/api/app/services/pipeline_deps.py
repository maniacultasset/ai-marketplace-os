"""`BackendPipelineDeps`: the backend side of the five AI-seam ports (CANON.md:119; S14b T7-T8).

The generation worker builds one per job with `build_pipeline_deps` and hands it to
`run_listing_pipeline`. It holds only what the ports need, as wire models and private slots:

- public, non-secret ids: `tenant_id`, `job_id`, `marketplace`;
- the loaded RuleSet and template, the product facts and the stored image reports (D6);
- the embedder, and `_ModelConfig`: a snapshot of the model settings with the model keys (D5).
  No `Settings` object, no marketplace account, no ORM instance is kept, so AI code that walks
  the object graph reaches no other secret (invariant 6; PLAN AC 3).

Ports:
- `retrieve_brand_chunks`: validates every input before any SQL (a foreign tenant is an internal
  error, `SeamPortError`), embeds the query through `embed_query` (S16 CP12: one `agent_runs` row
  per query embed, with the job's attempt and the request id), then
  `BrandChunkRepository.search` in one short unit of work.
- `validate_rules`: the Rules Engine on the preloaded RuleSet. A RuleSet holding a foreign rule, a
  listing of another scope or any engine exception gives an `INTERNAL_ERROR` report: never a
  pass (invariant 2; S14a hand-off). S18 (D8): calls 1..3 run the engine on the precomputed
  `for_rules` product mapping (CP13: never the full facts) and are recorded (`rules_calls`); a
  4th call gets an `INTERNAL_ERROR` report without evaluating (the cap lives in the graph,
  this is the backstop).
- `validate_images`: the stored reports of the listing's images, no I/O, no model.
- `get_model_credentials`: `generation` → the `LLM_PROVIDER` key and `LLM_MODEL_DEFAULT`;
  `vision` → the `LLM_PROVIDER_VISION` key (OpenAI only: DeepSeek has no vision model) and
  `LLM_MODEL_VISION` (B2, ai-deps C18); `embedding` → always the OpenAI key (ENV.md:86,
  BA:1538). Never a marketplace credential.
- `emit_node_event`: `started` writes `job_runs.node` on the running job (never
  `listings.status`) and opens span `pipeline.<node>` (attributes `job_id`, `tenant_id`, `node`
  only); `ok`/`failed` ends it with that status. `close_spans()` ends any left open. `detail` is
  only logged, redacted and cut to 2 KiB, never a span attribute.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, cast, get_args
from uuid import UUID

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, Marketplace, PipelineStage, RetryClass, Severity
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.images import ImageReport
from mkos_schemas.jobs import BrandChunkDTO, BrandChunkType, ModelCredentials
from mkos_schemas.listing import CanonicalListing
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode
from pydantic import SecretStr

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redaction import redact
from app.core.request_context import current_request_id, new_request_id
from app.repositories.uow import tenant_uow
from app.services.embeddings import (
    Embedder,
    EmbedderUnavailable,
    EmbeddingFailed,
    embed_query,
    get_embedder,
)
from app.services.rules_engine import evaluate
from app.services.rules_loader import LoadedRules

log = get_logger(__name__)

QUERY_MAX_CHARS: Final = 2000
K_MIN: Final = 1
K_MAX: Final = 50
DETAIL_MAX_BYTES: Final = 2048
LOCAL_PLACEHOLDER_KEY: Final = "local_only_model_key_unset"
CHUNK_TYPES: Final[frozenset[str]] = frozenset(get_args(BrandChunkType))
PURPOSES: Final = ("generation", "vision", "embedding")
NODE_STATUSES: Final = frozenset({"started", "ok", "failed"})
Purpose = Literal["generation", "vision", "embedding"]
MAX_RULES_CALLS: Final = 3
TRACER_NAME: Final = "app"
_tracer: trace.Tracer = trace.get_tracer(TRACER_NAME)


def use_tracer(tracer: trace.Tracer) -> None:
    """Install the tracer the node spans use (the worker's provider; an in-memory one in tests)."""
    global _tracer
    _tracer = tracer


@dataclass(frozen=True, slots=True)
class RulesCall:
    """One recorded `validate_rules` port call (D7: one `rules_engine` run each)."""

    attempt: int
    report: ErrorReport
    duration_ms: int


class SeamPortError(Exception):
    """A port call the backend refuses. The message holds the code and the field name only."""

    def __init__(self, code: ErrorCode, field: str) -> None:
        super().__init__(f"{code.value} at {field}")
        self.code = code
        self.field = field


def _secret(value: str | None) -> SecretStr | None:
    if value is None or not value.strip():
        return None
    return SecretStr(value)


def _provider_key(settings: Settings, provider: Literal["openai", "deepseek"]) -> SecretStr | None:
    return _secret(settings.openai_api_key if provider == "openai" else settings.deepseek_api_key)


@dataclass(frozen=True)
class _ModelConfig:
    """The model settings the credential port needs, copied once (no `Settings` is kept)."""

    app_env: str
    provider: Literal["openai", "deepseek"]
    generation_model: str
    vision_provider: Literal["openai"]
    vision_model: str
    embedding_model: str
    timeout_seconds: float
    generation_key: SecretStr | None = field(repr=False)
    vision_key: SecretStr | None = field(repr=False)
    embedding_key: SecretStr | None = field(repr=False)

    @classmethod
    def from_settings(cls, settings: Settings) -> _ModelConfig:
        return cls(
            app_env=settings.app_env,
            provider=settings.llm_provider,
            generation_model=settings.llm_model_default,
            vision_provider=settings.llm_provider_vision,
            vision_model=settings.llm_model_vision,
            embedding_model=settings.embedding_model,
            timeout_seconds=float(settings.llm_timeout_seconds),
            generation_key=_provider_key(settings, settings.llm_provider),
            vision_key=_provider_key(settings, settings.llm_provider_vision),
            embedding_key=_secret(settings.openai_api_key),
        )


def _internal_report(listing: CanonicalListing) -> ErrorReport:
    """`INTERNAL_ERROR` on the whole listing, `source=system` (C2): the run fails, never passes."""
    meta = ERROR_CODE_META[ErrorCode.INTERNAL_ERROR]
    retryable = meta.retry_class in (RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT)
    return ErrorReport(
        status="failed",
        error_code=ErrorCode.INTERNAL_ERROR,
        retryable=retryable,
        errors=[
            ErrorItem(
                field="listing",
                error_code=ErrorCode.INTERNAL_ERROR,
                severity=Severity.ERROR,
                message="The rules check could not run.",
                source=ErrorSource.SYSTEM,
                retryable=retryable,
                retry_class=meta.retry_class,
            )
        ],
        listing_id=listing.listing_id,
    )


class BackendPipelineDeps:
    """Implements `mkos_schemas.pipeline.PipelineDeps` (see the module docstring)."""

    __slots__ = (
        "_attempt",
        "_embedder",
        "_image_reports",
        "_job_id",
        "_marketplace",
        "_models",
        "_product",
        "_rules",
        "_rules_calls",
        "_spans",
        "_tenant_id",
        "_user_id",
        "_validate_calls",
    )

    def __init__(
        self,
        *,
        tenant_id: UUID,
        job_id: UUID,
        user_id: UUID | None,
        marketplace: Marketplace,
        rules: LoadedRules,
        product: Mapping[str, object] | None,
        image_reports: Mapping[UUID, ImageReport],
        embedder: Embedder | None,
        models: _ModelConfig,
        attempt: int = 1,
    ) -> None:
        self._tenant_id = tenant_id
        self._job_id = job_id
        self._user_id = user_id
        self._marketplace = marketplace
        self._rules = rules
        self._product = product
        self._image_reports = dict(image_reports)
        self._embedder = embedder
        self._models = models
        self._attempt = attempt
        self._validate_calls = 0
        self._rules_calls: list[RulesCall] = []
        self._spans: dict[str, Span] = {}

    def __repr__(self) -> str:
        return f"BackendPipelineDeps(tenant_id={self._tenant_id}, job_id={self._job_id})"

    @property
    def tenant_id(self) -> UUID:
        return self._tenant_id

    @property
    def job_id(self) -> UUID:
        return self._job_id

    @property
    def marketplace(self) -> Marketplace:
        return self._marketplace

    @property
    def rules_calls(self) -> tuple[RulesCall, ...]:
        """The engine runs of calls 1..3, in order (read-only)."""
        return tuple(self._rules_calls)

    @property
    def validate_calls(self) -> int:
        """Every `validate_rules` call, the refused ones past the cap included."""
        return self._validate_calls

    # ---- retrieve_brand_chunks (D4) ----

    def _check_search(
        self, tenant_id: UUID, query: object, k: object, types: Sequence[str] | None
    ) -> None:
        if tenant_id != self._tenant_id:
            raise SeamPortError(ErrorCode.INTERNAL_ERROR, "tenant_id")
        if not isinstance(query, str) or not query.strip() or len(query) > QUERY_MAX_CHARS:
            raise SeamPortError(ErrorCode.VALIDATION_FAILED, "query")
        if isinstance(k, bool) or not isinstance(k, int) or not K_MIN <= k <= K_MAX:
            raise SeamPortError(ErrorCode.VALIDATION_FAILED, "k")
        if types is not None and (
            isinstance(types, str)
            or len(types) == 0
            or any(not isinstance(t, str) or t not in CHUNK_TYPES for t in types)
        ):
            raise SeamPortError(ErrorCode.VALIDATION_FAILED, "types")
        if self._embedder is None:
            raise SeamPortError(ErrorCode.MODEL_UNAVAILABLE, "embedding")

    async def retrieve_brand_chunks(
        self,
        tenant_id: UUID,
        query: str,
        k: int = 8,
        types: Sequence[str] | None = None,
    ) -> Sequence[BrandChunkDTO]:
        self._check_search(tenant_id, query, k, types)
        embedder = self._embedder
        if embedder is None:  # _check_search refused it already; this narrows the type
            raise SeamPortError(ErrorCode.MODEL_UNAVAILABLE, "embedding")
        try:
            vector = await embed_query(
                embedder,
                query,
                tenant_id=self._tenant_id,
                job_id=self._job_id,
                attempt=self._attempt,
                request_id=current_request_id() or new_request_id(),
            )
        except EmbeddingFailed as exc:
            raise SeamPortError(exc.code, "embedding") from None
        wanted = None if types is None else cast(list[BrandChunkType], list(types))
        async with tenant_uow(self._tenant_id, user_id=self._user_id) as uow:
            return await uow.brand_chunks.search(vector, k, wanted)

    # ---- validate_rules (D7) ----

    async def validate_rules(self, listing: CanonicalListing) -> ErrorReport:
        self._validate_calls += 1
        if self._validate_calls > MAX_RULES_CALLS:
            log.warning("seam.validate_rules_cap", job_id=str(self._job_id))
            return _internal_report(listing)
        started = time.monotonic()
        # the synchronous engine off the event loop (KNOWN-ISSUES §E, S14b): the worker's
        # heartbeats and other jobs keep running while a large ruleset is evaluated
        report = await asyncio.to_thread(self._evaluate, listing)
        self._rules_calls.append(
            RulesCall(
                attempt=self._validate_calls,
                report=report,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        )
        return report

    def _evaluate(self, listing: CanonicalListing) -> ErrorReport:
        ruleset = self._rules.ruleset
        try:
            if any(r.tenant_id not in (None, self._tenant_id) for r in ruleset.rules):
                raise ValueError("the RuleSet holds another tenant's rule")
            if (
                listing.marketplace is not ruleset.marketplace
                or listing.category != ruleset.category
            ):
                raise ValueError("the listing is outside the RuleSet's scope")
            return evaluate(listing, ruleset, template=self._rules.template, product=self._product)
        except Exception as exc:  # fail closed: an internal error is a failed report, never a pass
            log.error(
                "seam.validate_rules_failed", error=type(exc).__name__, job_id=str(self._job_id)
            )
            return _internal_report(listing)

    # ---- validate_images (D6) ----

    async def validate_images(self, listing: CanonicalListing) -> list[ImageReport]:
        if listing.marketplace is not self._marketplace:
            return []
        reports = self._image_reports
        return [reports[i.image_id] for i in listing.images if i.image_id in reports]

    # ---- get_model_credentials (D5) ----

    async def get_model_credentials(self, purpose: Purpose) -> ModelCredentials:
        models = self._models
        if purpose not in PURPOSES:
            raise SeamPortError(ErrorCode.VALIDATION_FAILED, "purpose")
        provider: Literal["openai", "deepseek"]
        if purpose == "embedding":
            provider, key, model = "openai", models.embedding_key, models.embedding_model
        elif purpose == "vision":
            provider, key, model = models.vision_provider, models.vision_key, models.vision_model
        else:
            provider, key, model = models.provider, models.generation_key, models.generation_model
        if key is None:
            if models.app_env != "local":
                raise SeamPortError(ErrorCode.MODEL_UNAVAILABLE, purpose)
            key = SecretStr(LOCAL_PLACEHOLDER_KEY)
        return ModelCredentials(
            provider=provider,
            api_key=key,
            api_base=None,
            model=model,
            timeout_seconds=models.timeout_seconds,
        )

    # ---- emit_node_event ----

    async def emit_node_event(
        self,
        node: PipelineStage,
        status: Literal["started", "ok", "failed"],
        detail: dict[str, Any] | None = None,
    ) -> None:
        try:
            stage = PipelineStage(node)
        except ValueError:
            raise ValueError("node must be a PipelineStage") from None
        if status not in NODE_STATUSES:
            raise ValueError("status must be started, ok or failed")
        shown = None
        if detail is not None:
            text = json.dumps(redact(detail), default=str, ensure_ascii=False, sort_keys=True)
            shown = text.encode("utf-8")[:DETAIL_MAX_BYTES].decode("utf-8", "ignore")
        log.info(
            "seam.node_event",
            job_id=str(self._job_id),
            node=stage.value,
            status=status,
            detail=shown,
        )
        self._span_event(stage.value, status)
        if status != "started":
            return
        async with tenant_uow(self._tenant_id, user_id=self._user_id) as uow:
            moved = await uow.job_runs.set_node(self._job_id, stage.value, attempt=self._attempt)
        if not moved:
            log.warning("seam.node_event_dropped", job_id=str(self._job_id), node=stage.value)

    def _span_event(self, node: str, status: str) -> None:
        if status == "started":
            old = self._spans.pop(node, None)
            if old is not None:
                old.end()
            self._spans[node] = _tracer.start_span(
                f"pipeline.{node}",
                attributes={
                    "job_id": str(self._job_id),
                    "tenant_id": str(self._tenant_id),
                    "node": node,
                },
            )
            return
        span = self._spans.pop(node, None)
        if span is None:
            return
        span.set_status(Status(StatusCode.OK if status == "ok" else StatusCode.ERROR))
        span.end()

    def close_spans(self) -> None:
        """End every span a node left open (a pipeline that raised mid-node)."""
        for span in self._spans.values():
            span.set_status(Status(StatusCode.ERROR))
            span.end()
        self._spans.clear()


async def build_pipeline_deps(
    *,
    settings: Settings,
    tenant_id: UUID,
    job_id: UUID,
    user_id: UUID | None,
    product_id: UUID,
    marketplace: Marketplace,
    rules: LoadedRules,
    product: Mapping[str, object] | None = None,
) -> BackendPipelineDeps:
    """One short unit of work preloads the product's stored image reports (D6)."""
    market = Marketplace(marketplace)
    async with tenant_uow(tenant_id, user_id=user_id) as uow:
        reports = await uow.images.reports_for_product(product_id, market.value)
        job = await uow.job_runs.get_by_id(job_id)
    attempt = job.attempt if job is not None and 1 <= job.attempt <= 3 else 1
    try:
        embedder: Embedder | None = get_embedder(settings)
    except EmbedderUnavailable:
        embedder = None
    return BackendPipelineDeps(
        tenant_id=tenant_id,
        job_id=job_id,
        user_id=user_id,
        marketplace=market,
        rules=rules,
        product=product,
        image_reports=reports,
        embedder=embedder,
        models=_ModelConfig.from_settings(settings),
        attempt=attempt,
    )
