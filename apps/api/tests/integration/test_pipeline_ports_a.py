"""S14b T7 (AC 3; D4, D5, C8): `BackendPipelineDeps` construction, `retrieve_brand_chunks` and
`get_model_credentials`, and the object graph the AI code can reach (sockets: loopback only)."""

from __future__ import annotations

import dataclasses
import types
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from . import factories as f
from .auth_support import World
from .conftest import LOCAL_HOSTS
from .products_support import statements
from .seam_support import CATEGORY, Conn, add_product

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


def rules() -> Any:
    from app.services.rules_loader import LoadedRules
    from mkos_schemas.rules import RuleSet

    ruleset = RuleSet(marketplace="amazon", category=CATEGORY, loaded_at=datetime.now(UTC))
    return LoadedRules(ruleset=ruleset, template=None)


def settings(app_env: str = "local", **over: Any) -> Any:
    """A `Settings` built from keywords (no `.env`, no process env file)."""
    import os

    from app.core.config import Settings

    base: dict[str, Any] = {"app_env": app_env, "database_url": os.environ["DATABASE_URL"]}
    return Settings(_env_file=None, **{**base, **over})


async def build(world: World, conn: Conn, cfg: Any = None, slug: str = "acme", **over: Any) -> Any:
    from app.services.pipeline_deps import build_pipeline_deps

    tenant = world.tenant(slug)
    values: dict[str, Any] = {
        "settings": cfg if cfg is not None else settings(),
        "tenant_id": tenant,
        "job_id": uuid.uuid4(),
        "user_id": world.user(f"owner@{slug}.test"),
        "product_id": add_product(conn, tenant),
        "marketplace": "amazon",
        "rules": rules(),
        "critical_keys": frozenset(),
        "rejections": {},
        **over,
    }
    return await build_pipeline_deps(**values)


def chunk(conn: Conn, tenant: uuid.UUID, text: str, **columns: Any) -> uuid.UUID:
    from app.core.vectors import hash_vector

    vector = "[" + ",".join(repr(v) for v in hash_vector(text)) + "]"
    return f.brand_chunk(
        conn, tenant, text=text, embedding=vector, source_type="brand_profile", **columns
    )


# ---- retrieve_brand_chunks ----


async def test_port_retrieve_brand_chunks_own_tenant_only(
    products_world: World, auth_db: Conn
) -> None:
    acme, bharat = products_world.tenant("acme"), products_world.tenant("bharat")
    mine = {chunk(auth_db, acme, f"acme guidance {n}") for n in range(3)}
    chunk(auth_db, bharat, "bharat guidance")
    deps = await build(products_world, auth_db)
    found = await deps.retrieve_brand_chunks(acme, "guidance", k=8)
    assert {c.id for c in found} == mine
    assert {c.tenant_id for c in found} == {acme}
    typed = await deps.retrieve_brand_chunks(acme, "guidance", k=8, types=["glossary"])
    assert typed == []


async def test_port_retrieve_brand_chunks_other_tenant_raises_before_sql(
    products_world: World, auth_db: Conn
) -> None:
    from app.services.pipeline_deps import SeamPortError
    from mkos_schemas.codes import ErrorCode

    chunk(auth_db, products_world.tenant("bharat"), "bharat guidance")
    deps = await build(products_world, auth_db)
    with statements() as seen, pytest.raises(SeamPortError) as caught:
        await deps.retrieve_brand_chunks(products_world.tenant("bharat"), "guidance")
    assert seen == []
    assert caught.value.code is ErrorCode.INTERNAL_ERROR


@pytest.mark.parametrize(
    ("query", "k", "kinds", "field"),
    [
        ("   ", 8, None, "query"),
        ("x" * 2001, 8, None, "query"),
        ("ok", 0, None, "k"),
        ("ok", 51, None, "k"),
        ("ok", True, None, "k"),
        ("ok", 8, [], "types"),
        ("ok", 8, ["bogus"], "types"),
    ],
    ids=["blank", "2001", "k0", "k51", "kTrue", "types_empty", "types_bogus"],
)
async def test_port_retrieve_brand_chunks_input_refused(
    products_world: World, auth_db: Conn, query: str, k: Any, kinds: Any, field: str
) -> None:
    from app.services.pipeline_deps import SeamPortError
    from mkos_schemas.codes import ErrorCode

    deps = await build(products_world, auth_db)
    with statements() as seen, pytest.raises(SeamPortError) as caught:
        await deps.retrieve_brand_chunks(products_world.tenant("acme"), query, k=k, types=kinds)
    assert seen == []
    assert (caught.value.code, caught.value.field) == (ErrorCode.VALIDATION_FAILED, field)
    assert query not in str(caught.value) or query == "ok"


async def test_port_retrieve_brand_chunks_model_unavailable_outside_local(
    products_world: World, auth_db: Conn
) -> None:
    from app.services.pipeline_deps import SeamPortError
    from mkos_schemas.codes import ErrorCode

    chunk(auth_db, products_world.tenant("acme"), "acme guidance")  # SI10: chunks need the embed
    deps = await build(products_world, auth_db, settings("staging"))
    with pytest.raises(SeamPortError) as caught:
        await deps.retrieve_brand_chunks(products_world.tenant("acme"), "guidance")
    assert caught.value.code is ErrorCode.MODEL_UNAVAILABLE


async def test_port_retrieve_brand_chunks_unicode_query(
    products_world: World, auth_db: Conn
) -> None:
    acme = products_world.tenant("acme")
    chunk(auth_db, acme, "कुर्ता सेट guidance")
    deps = await build(products_world, auth_db)
    found = await deps.retrieve_brand_chunks(acme, "कुर्ता सेट 🎉", k=1)
    assert len(found) == 1


# ---- get_model_credentials (D5) ----


@pytest.mark.parametrize(
    ("purpose", "provider", "model", "key"),
    [
        ("generation", "openai", "gpt-4o-mini", "sk-openai"),
        ("vision", "openai", "gpt-4o-mini", "sk-openai"),
        ("embedding", "openai", "text-embedding-3-small", "sk-openai"),
    ],
)
async def test_port_get_model_credentials_purposes(
    products_world: World, auth_db: Conn, purpose: str, provider: str, model: str, key: str
) -> None:
    deps = await build(products_world, auth_db, settings(openai_api_key="sk-openai"))
    creds = await deps.get_model_credentials(purpose)
    assert (creds.provider, creds.model, creds.api_key.get_secret_value()) == (provider, model, key)
    assert creds.api_base is None and creds.timeout_seconds == 40


async def test_port_get_model_credentials_through_the_openrouter_gateway(
    products_world: World, auth_db: Conn
) -> None:
    """ADR-0024: under LLM_GATEWAY=openrouter every purpose carries the one key and the pinned
    base; the direct keys are never handed out."""
    cfg = settings(
        "staging",
        llm_gateway="openrouter",
        llm_provider="deepseek",
        llm_model_default="deepseek/deepseek-chat",
        openai_api_key="sk-openai",
        deepseek_api_key="sk-deep",
        openrouter_api_key="sk-router",
    )
    deps = await build(products_world, auth_db, cfg)
    generation = await deps.get_model_credentials("generation")
    assert (generation.provider, generation.model) == ("deepseek", "deepseek/deepseek-chat")
    for purpose in ("generation", "vision", "embedding"):
        creds = await deps.get_model_credentials(purpose)
        assert creds.api_base == "https://openrouter.ai/api/v1", purpose
        assert creds.api_key.get_secret_value() == "sk-router", purpose
    embedding = await deps.get_model_credentials("embedding")
    assert embedding.model == "openai/text-embedding-3-small"


async def test_port_get_model_credentials_local_placeholder_when_unset(
    products_world: World, auth_db: Conn
) -> None:
    deps = await build(products_world, auth_db, settings())
    creds = await deps.get_model_credentials("generation")
    assert creds.api_key.get_secret_value() == "local_only_model_key_unset"


@pytest.mark.parametrize("purpose", ["generation", "vision", "embedding"])
async def test_port_get_model_credentials_model_unavailable_outside_local(
    products_world: World, auth_db: Conn, purpose: str
) -> None:
    from app.services.pipeline_deps import SeamPortError
    from mkos_schemas.codes import ErrorCode

    deps = await build(products_world, auth_db, settings("staging"))
    with pytest.raises(SeamPortError) as caught:
        await deps.get_model_credentials(purpose)
    assert caught.value.code is ErrorCode.MODEL_UNAVAILABLE


@pytest.mark.parametrize("purpose", ["escalation", "publish", ""])
async def test_port_get_model_credentials_unknown_purpose_refused(
    products_world: World, auth_db: Conn, purpose: str
) -> None:
    from app.services.pipeline_deps import SeamPortError
    from mkos_schemas.codes import ErrorCode

    deps = await build(products_world, auth_db, settings(openai_api_key="sk-openai"))
    with pytest.raises(SeamPortError) as caught:
        await deps.get_model_credentials(purpose)
    assert caught.value.code is ErrorCode.VALIDATION_FAILED


async def test_port_get_model_credentials_embedding_is_openai_under_deepseek(
    products_world: World, auth_db: Conn
) -> None:
    cfg = settings(llm_provider="deepseek", openai_api_key="sk-openai", deepseek_api_key="sk-deep")
    deps = await build(products_world, auth_db, cfg)
    generation = await deps.get_model_credentials("generation")
    embedding = await deps.get_model_credentials("embedding")
    assert (generation.provider, generation.api_key.get_secret_value()) == ("deepseek", "sk-deep")
    assert (embedding.provider, embedding.api_key.get_secret_value()) == ("openai", "sk-openai")


async def test_port_get_model_credentials_vision_settings_through_the_builder(
    products_world: World, auth_db: Conn
) -> None:
    """B2 (ai-deps C18): DeepSeek has no vision model, so vision keeps its own OpenAI settings."""
    cfg = settings(
        llm_provider="deepseek",
        openai_api_key="sk-openai",
        deepseek_api_key="sk-deep",
        llm_model_vision="gpt-4o",
    )
    deps = await build(products_world, auth_db, cfg)
    vision = await deps.get_model_credentials("vision")
    assert (vision.provider, vision.model, vision.api_key.get_secret_value()) == (
        "openai",
        "gpt-4o",
        "sk-openai",
    )


SECRET_FIELDS = (
    "jwt_secret",
    "alembic_database_url",
    "amazon_client_secret",
    "flipkart_client_secret",
    "s3_secret_key",
    "kms_local_dev_key",
    "kms_key_uri",
    "sentry_dsn",
    "admin_session_secret",
    "alert_slack_webhook",
)


def sentinel_settings(provider: str = "openai") -> tuple[Any, dict[str, str]]:
    import os

    values = {name: f"sentinel_{name}_value_at_least_32_characters" for name in SECRET_FIELDS}
    values["alembic_database_url"] = "postgresql+psycopg://sentinel_alembic:pw@127.0.0.1:1/x"
    values["sentry_dsn"] = "https://sentinel_sentry@o0.ingest.sentry.io/0"
    values["kms_key_uri"] = "projects/sentinel_kms/keyRings/k/cryptoKeys/c"
    keys = {"openai_api_key": "sentinel_openai_key", "deepseek_api_key": "sentinel_deepseek_key"}
    cfg = settings(llm_provider=provider, **values, **keys)
    values["database_url"] = os.environ["DATABASE_URL"]
    return cfg, {**values, **keys}


async def test_port_get_model_credentials_holds_no_other_secret(
    products_world: World, auth_db: Conn
) -> None:
    cfg, sentinels = sentinel_settings()
    deps = await build(products_world, auth_db, cfg)
    for purpose in ("generation", "vision", "embedding"):
        creds = await deps.get_model_credentials(purpose)
        dumped = creds.model_dump_json() + creds.api_key.get_secret_value()
        others = {k: v for k, v in sentinels.items() if k != "openai_api_key"}
        assert [k for k, v in others.items() if v in dumped] == []


async def test_port_get_model_credentials_repr_hides_key(
    products_world: World, auth_db: Conn
) -> None:
    deps = await build(products_world, auth_db, settings(openai_api_key="sk-openai-visible"))
    creds = await deps.get_model_credentials("generation")
    for text in (repr(creds), str(creds), creds.model_dump_json(), repr(deps)):
        assert "sk-openai-visible" not in text
    assert "sk-openai-visible" not in repr(deps._models)


# ---- the object graph (C8) ----


def walk(root: object) -> tuple[list[str], list[object]]:
    """Every string and bytes value reachable from `root`, and every object visited."""
    texts: list[str] = []
    seen: dict[int, object] = {}
    stack: list[object] = [root]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen[id(node)] = node
        if isinstance(node, str):
            texts.append(node)
            continue
        if isinstance(node, bytes | bytearray):
            texts.append(bytes(node).decode("latin-1"))
            continue
        if isinstance(node, SecretStr):
            stack.append(node.get_secret_value())
            continue
        if isinstance(node, int | float | bool | type(None) | uuid.UUID | datetime | type):
            continue
        if isinstance(node, Mapping):
            stack.extend(node.keys())
            stack.extend(node.values())
            continue
        if isinstance(node, list | tuple | set | frozenset):
            stack.extend(node)
            continue
        if isinstance(node, types.MethodType):
            stack.extend([node.__self__, node.__func__])
            continue
        if isinstance(node, types.FunctionType):
            stack.extend(c.cell_contents for c in node.__closure__ or ())
            continue
        if isinstance(node, BaseModel):
            stack.extend(getattr(node, name) for name in type(node).model_fields)
            continue
        if dataclasses.is_dataclass(node):
            stack.extend(getattr(node, fl.name) for fl in dataclasses.fields(node))
        for cls in type(node).__mro__:
            for slot in getattr(cls, "__slots__", ()):
                if hasattr(node, slot):
                    stack.append(getattr(node, slot))
        if hasattr(node, "__dict__"):
            stack.extend(vars(node).values())
    return texts, list(seen.values())


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
async def test_pipeline_deps_object_graph_holds_no_secret(
    products_world: World, auth_db: Conn, provider: str
) -> None:
    from app.core.config import Settings
    from sqlalchemy.orm import DeclarativeBase

    acme = products_world.tenant("acme")
    token = b"sentinel_token_enc_bytes_0123456789"
    f.marketplace_account(auth_db, acme, token_enc=token)
    from app.core.resolved_facts import Compliance, PartyFacts, ProductFacts, for_rules

    cfg, sentinels = sentinel_settings(provider)
    # S18 T12 (CP13): the deps hold the `for_rules` facts only, never party contacts or GSTIN
    party = PartyFacts(
        legal_name="Sentinel Maker Pvt Ltd",
        address="sentinel_party_address_line",
        email="sentinel_party@email.example",
        phone="+91-sentinel-phone",
        gstin="29SENTINEL1Z9",
    )
    facts = ProductFacts(
        product_id=uuid.uuid4(),
        sku="SENTINEL-1",
        level="standalone",
        compliance=Compliance(manufacturer=party, consumer_care=party),
    )
    deps = await build(products_world, auth_db, cfg, product=for_rules(facts))
    texts, objects = walk(deps)
    blob = "\n".join(texts)
    for contact in ("sentinel_party_address_line", "sentinel_party@email.example"):
        assert contact not in blob
    assert "+91-sentinel-phone" not in blob and "29SENTINEL1Z9" not in blob
    assert "Sentinel Maker Pvt Ltd" in blob
    allowed = {"openai_api_key"} | ({"deepseek_api_key"} if provider == "deepseek" else set())
    reached = {k for k, v in sentinels.items() if v in blob}
    assert reached == allowed  # exactly the D5 model keys (C8)
    assert token.decode("latin-1") not in blob
    assert not any(isinstance(o, Settings | DeclarativeBase) for o in objects)
    assert not any(type(o).__name__ == "MarketplaceAccount" for o in objects)
    assert {deps.tenant_id, deps.job_id} and deps.marketplace.value == "amazon"


def test_pipeline_deps_satisfies_protocol() -> None:
    from app.services.pipeline_deps import BackendPipelineDeps
    from mkos_schemas.pipeline import PipelineDeps

    def accepts(_: PipelineDeps) -> None:
        return None

    accepts(BackendPipelineDeps.__new__(BackendPipelineDeps))


# ---- SI10 T9 (AC11, AC8): one query embed per run, or none ----

# The three concurrent calls of the pipeline's retrieve_brand node (one query, three type sets).
TYPE_SETS = (["tone_rule", "example"], ["glossary"], ["compliance"])


class CountingEmbedder:
    """The local hash embedder with a call counter, or a scripted failure."""

    model = "text-embedding-3-small"

    def __init__(self, failure: Exception | None = None) -> None:
        self.calls = 0
        self.failure = failure

    async def embed(self, texts: Any) -> Any:
        import asyncio

        from app.core.vectors import hash_vector
        from app.services.embeddings import EmbeddingBatch

        self.calls += 1
        await asyncio.sleep(0.01)  # concurrent callers overlap
        if self.failure is not None:
            raise self.failure
        return EmbeddingBatch(
            vectors=[hash_vector(t) for t in texts], input_tokens=len(texts), model=self.model
        )


async def _with_embedder(world: World, conn: Conn, embedder: Any) -> Any:
    deps = await build(world, conn)
    deps._embedder = embedder
    return deps


async def _three(deps: Any, tenant: uuid.UUID) -> list[Any]:
    import asyncio

    return list(
        await asyncio.gather(
            *(deps.retrieve_brand_chunks(tenant, "kurta guidance", 8, t) for t in TYPE_SETS),
            return_exceptions=True,
        )
    )


def _runs(conn: Conn, tenant: uuid.UUID) -> int:
    row = conn.execute("SELECT count(*) FROM agent_runs WHERE tenant_id = %s", [tenant]).fetchone()
    assert row is not None
    return int(row[0])


async def test_retrieve_no_chunks_no_embed(products_world: World, auth_db: Conn) -> None:
    acme = products_world.tenant("acme")
    embedder = CountingEmbedder()
    deps = await _with_embedder(products_world, auth_db, embedder)
    assert await _three(deps, acme) == [[], [], []]
    assert embedder.calls == 0
    assert _runs(auth_db, acme) == 0
    assert deps.embed_refusal is None


async def test_retrieve_with_chunks_one_embed(products_world: World, auth_db: Conn) -> None:
    acme = products_world.tenant("acme")
    tone = chunk(auth_db, acme, "acme tone", type="tone_rule")
    gloss = chunk(auth_db, acme, "acme glossary", type="glossary")
    embedder = CountingEmbedder()
    deps = await _with_embedder(products_world, auth_db, embedder)
    first, second, third = await _three(deps, acme)
    assert embedder.calls == 1
    assert _runs(auth_db, acme) == 1
    assert {c.id for c in first} == {tone}
    assert {c.id for c in second} == {gloss}
    assert third == []


@pytest.mark.parametrize(
    ("failure", "code", "refusal"),
    [
        ("credit", "AI_CREDIT_EXHAUSTED", "credit"),
        ("wire403", "MODEL_UNAVAILABLE", "key_suspect"),
        ("body403", "MODEL_UNAVAILABLE", None),
    ],
)
async def test_retrieve_failure_shared_by_all_three(
    products_world: World, auth_db: Conn, failure: str, code: str, refusal: str | None
) -> None:
    from app.services.embeddings import EmbeddingFailed
    from app.services.pipeline_deps import SeamPortError
    from mkos_schemas.codes import ErrorCode

    failures = {
        "credit": EmbeddingFailed(ErrorCode.AI_CREDIT_EXHAUSTED, retryable=False, status=402),
        "wire403": EmbeddingFailed(
            ErrorCode.MODEL_UNAVAILABLE, retryable=False, status=403, key_suspect=True
        ),
        "body403": EmbeddingFailed(ErrorCode.MODEL_UNAVAILABLE, retryable=False, status=403),
    }
    acme = products_world.tenant("acme")
    chunk(auth_db, acme, "acme tone")
    embedder = CountingEmbedder(failures[failure])
    deps = await _with_embedder(products_world, auth_db, embedder)
    results = await _three(deps, acme)
    assert embedder.calls == 1
    assert all(isinstance(r, SeamPortError) for r in results), results
    assert {r.code.value for r in results} == {code}
    assert len({id(r) for r in results}) == 3, "each caller gets its own exception"
    assert deps.embed_refusal == refusal


async def test_retrieve_old_model_or_inactive_chunks_count_as_none(
    products_world: World, auth_db: Conn
) -> None:
    acme = products_world.tenant("acme")
    chunk(auth_db, acme, "old model", embedding_model="text-embedding-ada-002")
    chunk(auth_db, acme, "retired", is_active=False)
    embedder = CountingEmbedder()
    deps = await _with_embedder(products_world, auth_db, embedder)
    assert await _three(deps, acme) == [[], [], []]
    assert embedder.calls == 0


async def test_retrieve_no_embedder_no_chunks_is_empty(
    products_world: World, auth_db: Conn
) -> None:
    deps = await build(products_world, auth_db, settings("staging"))
    assert await deps.retrieve_brand_chunks(products_world.tenant("acme"), "guidance") == []


async def test_retrieve_other_tenant_chunks_do_not_count(
    products_world: World, auth_db: Conn
) -> None:
    acme, bharat = products_world.tenant("acme"), products_world.tenant("bharat")
    chunk(auth_db, bharat, "bharat guidance")
    embedder = CountingEmbedder()
    deps = await _with_embedder(products_world, auth_db, embedder)
    assert await _three(deps, acme) == [[], [], []]
    assert embedder.calls == 0
