"""`audit_log` (DATABASE.md §5.1; KI-B tenant and FK fixes), `brand_documents` and `brand_chunks` (§5.5; D1, T66):
platform events and the actor CHECKs, the NO ACTION tenant FK, the object key, and the chunk
vocabulary, embedding and active-chunk key. The HNSW and partial indexes are 0002b."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import psycopg
import pytest

from . import factories as f
from .conftest import expect_sqlstate

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
TEN_MIB = 10 * 1024 * 1024


# ---- audit_log ----

AUDIT_REJECTS: dict[str, tuple[bool, dict[str, Any], str]] = {
    "user actor on a platform event": (
        False,
        {"actor_type": "user"},
        "ck_audit_log_platform_actor",
    ),
    "user actor without a user": (True, {"actor_type": "user"}, "ck_audit_log_user_actor"),
    "staff actor without an email": (True, {"actor_type": "staff"}, "ck_audit_log_staff_actor"),
    "actor_type": (True, {"actor_type": "bot"}, "ck_audit_log_actor_type"),
    "audit_reason_1001": (True, {"reason": "x" * 1001}, "ck_audit_log_reason"),
}


@pytest.mark.parametrize("case", sorted(AUDIT_REJECTS))
def test_checks_reject_audit_actor(db: Conn, case: str) -> None:
    with_tenant, columns, constraint = AUDIT_REJECTS[case]
    tenant_id = f.tenant(db) if with_tenant else None
    f.expect_violation(db, "23514", constraint, lambda: f.audit(db, tenant_id, **columns))


def test_audit_platform_event_allowed(db: Conn) -> None:
    f.audit(db, None, action="auth.login_failed", entity_type="user")
    f.audit(db, None, actor_type="staff", actor_email="ops@x.test", ip="203.0.113.7")
    tenant_id = f.tenant(db)
    user_id = f.member(db, tenant_id)
    f.audit(db, tenant_id, actor_type="user", actor_user_id=user_id, reason="x" * 1000)
    f.audit(
        db,
        tenant_id,
        before={"status": "needs_review"},
        after={"status": "approved"},
        entity_id=user_id,
        request_id="req-1",
        job_id="00000000-0000-0000-0000-00000000000e",
    )
    row = db.execute(
        "SELECT occurred_at = now() FROM audit_log WHERE tenant_id = %s LIMIT 1", (tenant_id,)
    ).fetchone()
    assert row == (True,)


def test_audit_tenant_fk_no_action(db: Conn) -> None:
    """KI-B: deleting a tenant that still has audit rows fails (the purge ADR is S10's)."""
    tenant_id = f.tenant(db)
    f.audit(db, tenant_id)
    expect_sqlstate(
        db,
        "23503",
        "DELETE FROM tenants WHERE id = %s",
        (tenant_id,),
        constraint="fk_audit_log_tenant_id_tenants",
    )
    row = db.execute(
        "SELECT confdeltype FROM pg_constraint WHERE conname = 'fk_audit_log_tenant_id_tenants'"
    ).fetchone()
    assert row == ("a",)  # NO ACTION


def test_audit_log_indexes_documented(db: Conn) -> None:
    rows: dict[str, str] = dict(
        db.execute(
            "SELECT indexrelid::regclass::text, pg_get_indexdef(indexrelid) FROM pg_index "
            "WHERE indrelid = 'audit_log'::regclass"
        ).fetchall()
    )
    tails = sorted(definition.split(" USING btree ")[1] for definition in rows.values())
    assert tails == sorted(
        [
            "(id)",
            "(tenant_id, occurred_at DESC, id DESC)",
            "(tenant_id, entity_type, entity_id, occurred_at DESC)",
            "(tenant_id, actor_user_id, occurred_at DESC)",
            "(occurred_at DESC) WHERE (tenant_id IS NULL)",
            "(job_id) WHERE (job_id IS NOT NULL)",
        ]
    )
    partitioned = db.execute(
        "SELECT relkind FROM pg_class WHERE oid = 'audit_log'::regclass"
    ).fetchone()
    assert partitioned == ("r",)  # not partitioned (SUPERSEDED.md)


# ---- brand_documents ----


def test_brand_documents_storage_key_column(db: Conn) -> None:
    """D1: the object key is `storage_key` (open decision 13), never `gcs_uri`."""
    columns = {
        str(r[0])
        for r in db.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = 'brand_documents'::regclass "
            "AND attnum > 0 AND NOT attisdropped"
        ).fetchall()
    }
    assert "storage_key" in columns
    assert "gcs_uri" not in columns


def test_brand_documents_keys(db: Conn) -> None:
    """One tenant never has two rows on one object key, nor two live copies of one file; the
    same key in another tenant is its own row (the images rule)."""
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    f.brand_document(db, tenant_a, storage_key="shared/guide.pdf", content_hash="h1")
    f.expect_violation(
        db,
        "23505",
        "uq_brand_documents_tenant_id_storage_key",
        lambda: f.brand_document(db, tenant_a, storage_key="shared/guide.pdf"),
    )
    f.expect_violation(
        db,
        "23505",
        "uq_brand_documents_tenant_id_content_hash",
        lambda: f.brand_document(db, tenant_a, content_hash="h1"),
    )
    f.brand_document(db, tenant_b, storage_key="shared/guide.pdf", content_hash="h1")
    db.execute("UPDATE brand_documents SET deleted_at = now() WHERE tenant_id = %s", (tenant_a,))
    f.brand_document(db, tenant_a, content_hash="h1")  # a re-upload after the delete


DOC_REJECTS: dict[str, tuple[dict[str, Any], str]] = {
    "status": ({"status": "done"}, "ck_brand_documents_status"),
    "failed without report": ({"status": "failed"}, "ck_brand_documents_error_report_json"),
    "report not an object": (
        {"status": "failed", "error_report_json": psycopg.types.json.Jsonb([])},
        "ck_brand_documents_error_report_json",
    ),
    "bytes 0": ({"bytes": 0}, "ck_brand_documents_bytes"),
    "bytes over 10 MiB": ({"bytes": TEN_MIB + 1}, "ck_brand_documents_bytes"),
    "chunk_count": ({"chunk_count": -1}, "ck_brand_documents_chunk_count"),
    "storage_key url": ({"storage_key": "gs://bucket/a.pdf"}, "ck_brand_documents_storage_key"),
    "storage_key traversal": ({"storage_key": "a/../b.pdf"}, "ck_brand_documents_storage_key"),
    "filename empty": ({"filename": ""}, "ck_brand_documents_filename"),
    "filename 256": ({"filename": "x" * 256}, "ck_brand_documents_filename"),
}


@pytest.mark.parametrize("case", sorted(DOC_REJECTS))
def test_checks_reject_brand_documents(db: Conn, case: str) -> None:
    columns, constraint = DOC_REJECTS[case]
    tenant_id = f.tenant(db)
    f.expect_violation(db, "23514", constraint, lambda: f.brand_document(db, tenant_id, **columns))


def test_checks_accept_brand_documents(db: Conn) -> None:
    tenant_id = f.tenant(db)
    for status in ("uploaded", "processing", "ready"):
        f.brand_document(db, tenant_id, status=status)
    f.brand_document(db, tenant_id, status="failed", error_report_json={"errors": []})
    f.brand_document(db, tenant_id, bytes=TEN_MIB, filename="x" * 255, chunk_count=12)


# ---- brand_chunks ----


@pytest.mark.parametrize("chunk_type", ["tone_rule", "glossary", "compliance", "example"])
def test_brand_chunks_type_vocab(db: Conn, chunk_type: str) -> None:
    f.brand_chunk(db, f.tenant(db), type=chunk_type)


def test_brand_chunks_type_rejects(db: Conn) -> None:
    tenant_id = f.tenant(db)
    f.expect_violation(
        db, "23514", "ck_brand_chunks_type", lambda: f.brand_chunk(db, tenant_id, type="faq")
    )


def test_brand_chunks_embedding_required_and_1536(db: Conn) -> None:
    tenant_id = f.tenant(db)
    expect_sqlstate(
        db,
        "23502",
        "INSERT INTO brand_chunks (tenant_id, type, text, embedding_model, content_hash, "
        "source_type, source_id) VALUES (%s, 'tone_rule', 't', 'm', 'h', 'brand_profile', %s)",
        (tenant_id, tenant_id),
    )
    error = expect_sqlstate(
        db,
        "22000",
        "INSERT INTO brand_chunks (tenant_id, type, text, embedding, embedding_model, "
        "content_hash, source_type, source_id) VALUES (%s, 'tone_rule', 't', '[1,2,3]', 'm', "
        "'h', 'brand_profile', %s)",
        (tenant_id, tenant_id),
    )
    assert "1536" in str(error)
    row = db.execute(
        "SELECT format_type(atttypid, atttypmod), attnotnull FROM pg_attribute "
        "WHERE attrelid = 'brand_chunks'::regclass AND attname = 'embedding'"
    ).fetchone()
    assert row == ("vector(1536)", True)


def test_brand_chunks_active_unique(db: Conn) -> None:
    tenant_id = f.tenant(db)
    source = UUID(int=424242)
    f.brand_chunk(db, tenant_id, source_id=source, chunk_index=3)
    f.expect_violation(
        db,
        "23505",
        "uq_brand_chunks_active_source",
        lambda: f.brand_chunk(db, tenant_id, source_id=source, chunk_index=3),
    )
    f.brand_chunk(db, tenant_id, source_id=source, chunk_index=3, embedding_model="model-2")
    db.execute(
        "UPDATE brand_chunks SET is_active = false WHERE source_id = %s AND chunk_index = 3 "
        "AND embedding_model = 'text-embedding-3-small'",
        (source,),
    )
    f.brand_chunk(db, tenant_id, source_id=source, chunk_index=3)  # the re-chunk
    f.brand_chunk(db, f.tenant(db), source_id=source, chunk_index=3)


CHUNK_REJECTS: dict[str, tuple[dict[str, Any], str]] = {
    "source_type": ({"source_type": "upload"}, "ck_brand_chunks_source_type"),
    "chunk_index": ({"chunk_index": -1}, "ck_brand_chunks_chunk_index"),
    "token_count": ({"token_count": -1}, "ck_brand_chunks_token_count"),
}


@pytest.mark.parametrize("case", sorted(CHUNK_REJECTS))
def test_checks_reject_brand_chunks(db: Conn, case: str) -> None:
    columns, constraint = CHUNK_REJECTS[case]
    tenant_id = f.tenant(db)
    f.expect_violation(db, "23514", constraint, lambda: f.brand_chunk(db, tenant_id, **columns))


# ---- cross-tenant FKs ----

Build = Callable[[Conn, UUID, UUID], None]


def _audit_actor(db: Conn, a: UUID, b: UUID) -> None:
    f.audit(db, a, actor_type="user", actor_user_id=f.member(db, b))


def _document_created_by(db: Conn, a: UUID, b: UUID) -> None:
    f.brand_document(db, a, created_by=f.member(db, b))


CROSS_TENANT: dict[str, tuple[Build, str]] = {
    "audit_log.actor_user_id": (_audit_actor, "fk_audit_log_actor_user_id"),
    "brand_documents.created_by": (_document_created_by, "fk_brand_documents_created_by"),
}
CROSS_TENANT_FK_COLUMNS = frozenset(CROSS_TENANT)


@pytest.mark.parametrize("column", sorted(CROSS_TENANT))
def test_0002_cross_tenant_fk_audit_brand(db: Conn, column: str) -> None:
    build, constraint = CROSS_TENANT[column]
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    f.expect_violation(db, "23503", constraint, lambda: build(db, tenant_a, tenant_b))
