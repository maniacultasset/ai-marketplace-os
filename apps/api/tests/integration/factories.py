"""Row factories for the 0001 schema tests. Each inserts one row on the given connection (the
caller's transaction) and returns its id. Keyword arguments override or add columns; a dict
value is sent as jsonb, a list as a PostgreSQL array (wrap it in `Jsonb` for a jsonb array)."""

from __future__ import annotations

import itertools
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

Conn = psycopg.Connection[tuple[Any, ...]]

_counter = itertools.count(1)


def _next() -> int:
    return next(_counter)


def fact(source: Any = "seller", captured_via: Any = "form", **fields: Any) -> dict[str, Any]:
    """One fact envelope (DATABASE.md §5.6). `value` defaults to "x"; pass `value=None` to omit
    it. A non-seller source gets an `image:` evidence ref unless one is given."""
    envelope: dict[str, Any] = {
        "value": "x",
        "source": source,
        "captured_via": captured_via,
        "updated_at": "2026-10-05T00:00:00Z",
    }
    if source != "seller":
        envelope["evidence_ref"] = "image:abc"
    if source in ("ai", "derived"):
        envelope["confidence"] = 0.9
    if captured_via == "agent":
        envelope["agent_run_id"] = "00000000-0000-0000-0000-000000000001"
    envelope.update(fields)
    return {key: value for key, value in envelope.items() if value is not None}


def _adapt(value: Any) -> Any:
    return Jsonb(value) if isinstance(value, dict) else value


def insert(conn: Conn, table: str, key: str = "id", **columns: Any) -> Any:
    """INSERT one row and return its `key` column."""
    names = list(columns)
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING {}").format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(name) for name in names),
        sql.SQL(", ").join(sql.Placeholder() for _ in names),
        sql.Identifier(key),
    )
    row = conn.execute(query, [_adapt(columns[name]) for name in names]).fetchone()
    assert row is not None
    return row[0]


def tenant(conn: Conn, **columns: Any) -> UUID:
    n = _next()
    defaults: dict[str, Any] = {"name": f"Tenant {n}", "slug": f"tenant-{n}", "status": "active"}
    return insert(conn, "tenants", **{**defaults, **columns})  # type: ignore[no-any-return]


def user(conn: Conn, **columns: Any) -> UUID:
    n = _next()
    defaults: dict[str, Any] = {"email": f"user{n}@x.test", "hashed_password": "argon2id-hash"}
    return insert(conn, "users", **{**defaults, **columns})  # type: ignore[no-any-return]


def member(conn: Conn, tenant_id: UUID, role: str = "owner", **columns: Any) -> UUID:
    """A new user with a membership in `tenant_id`; returns the user id (what `→ m` names)."""
    user_id = user(conn)
    insert(conn, "tenant_memberships", tenant_id=tenant_id, user_id=user_id, role=role, **columns)
    return user_id


def party(conn: Conn, tenant_id: UUID, **columns: Any) -> UUID:
    n = _next()
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "roles": ["manufacturer"],
        "legal_name": f"Party {n} Pvt Ltd",
        "source": "seller",
    }
    return insert(conn, "business_parties", **{**defaults, **columns})  # type: ignore[no-any-return]


def brand_profile(conn: Conn, tenant_id: UUID, **columns: Any) -> UUID:
    return insert(  # type: ignore[no-any-return]
        conn, "brand_profiles", key="tenant_id", tenant_id=tenant_id, **columns
    )


# Typed fact column → its `provenance` key (DATABASE.md §5.6 `ck_products_typed_facts_sourced`).
TYPED_FACT_KEYS: dict[str, str] = {
    "mrp": "mrp",
    "selling_price": "selling_price",
    "tax_rate": "tax_rate",
    "hsn_code": "hsn_code",
    "country_of_origin": "country_of_origin",
    "generic_name": "generic_name",
    "net_quantity": "net_quantity",
    "identifier_status": "identifier",
    "package_length_cm": "package_dimensions",
    "package_weight_g": "package_weight",
    "product_weight_g": "product_weight",
    "manufacturer_party_id": "manufacturer",
    "marketer_party_id": "marketer",
    "packer_party_id": "packer",
    "importer_party_id": "importer",
    "consumer_care_party_id": "consumer_care",
    "brand_name": "brand_name",
    "sub_category": "sub_category",
    "product_type": "product_type",
    "gender": "gender",
}


def product(
    conn: Conn,
    tenant_id: UUID,
    kind: str = "standalone",
    *,
    parent: UUID | None = None,
    **columns: Any,
) -> UUID:
    """A `style` (theme `size_color`), a `child` of `parent` or a `standalone` product. Unless
    `provenance` is given, it holds a seller fact for title, category and every typed fact
    column passed, which is what `ck_products_typed_facts_sourced` needs."""
    n = _next()
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "sku": f"SKU-{n}",
        "title": f"Product {n}",
        "category": "t_shirts",
    }
    if "provenance" not in columns:
        keys = ["title", "category"]
        keys += [key for col, key in TYPED_FACT_KEYS.items() if columns.get(col) is not None]
        defaults["provenance"] = {key: fact() for key in keys}
    if kind == "style":
        defaults["variation_theme"] = "size_color"
    elif kind == "child":
        assert parent is not None, "a child needs its style"
        defaults["parent_product_id"] = parent
        defaults["variant_attributes"] = {"size": f"s{n}"}
    else:
        assert kind == "standalone", kind
    return insert(conn, "products", **{**defaults, **columns})  # type: ignore[no-any-return]


def colourway(conn: Conn, tenant_id: UUID, style_id: UUID, **columns: Any) -> UUID:
    n = _next()
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "style_product_id": style_id,
        "attributes": {"color": fact(value=f"Colour {n}")},
        "position": 1,
    }
    return insert(conn, "product_colourways", **{**defaults, **columns})  # type: ignore[no-any-return]


def cell(conn: Conn, tenant_id: UUID, style_id: UUID, **columns: Any) -> UUID:
    """An `unavailable/not_produced` cell for size `s<n>` unless the columns say otherwise."""
    n = _next()
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "style_product_id": style_id,
        "size_key": f"s{n}",
        "availability": "unavailable",
        "unavailable_reason": "not_produced",
        "source": "seller",
    }
    return insert(conn, "variant_matrix_cells", **{**defaults, **columns})  # type: ignore[no-any-return]


# ---- 0002 (DATABASE.md §5.2-§5.7) ----

POLICY_REF: dict[str, Any] = {"source": "amazon_policy", "title": "Product title requirements"}


def rule_def(rule_type: str = "max_length", **fields: Any) -> dict[str, Any]:
    """A RuleDef body (`mkos_schemas.rules`); a platform rule cites its policy."""
    body: dict[str, Any] = {"type": rule_type, "message": "check {value}", "error_code": "X"}
    body.update(fields)
    return {key: value for key, value in body.items() if value is not None}


def rule(conn: Conn, tenant_id: UUID | None = None, **columns: Any) -> UUID:
    """A platform `max_length` rule on `content.title` unless the columns say otherwise; a
    tenant rule defaults to `banned_terms` (the brand-rule types, DATABASE.md §5.2)."""
    if tenant_id is None:
        default_def = rule_def("max_length", limit=200, policy_ref=POLICY_REF)
    else:
        default_def = rule_def("banned_terms", terms=["best"], pack="brand")
    body = columns.pop("rule_def", default_def)
    fields = body if isinstance(body, dict) else {}
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "marketplace": "amazon",
        "field": "content.title",
        "rule_type": fields.get("type", "max_length"),
        "subject": "",
        "pack": fields.get("pack", "core"),
        "rule_def": body,
    }
    return insert(conn, "rules", **{**defaults, **columns})  # type: ignore[no-any-return]


def expect_violation(conn: Conn, code: str, constraint: str, build: Any) -> None:
    """Run `build()` (row inserts) in a savepoint and assert it fails with `code` on
    `constraint`; the connection stays usable."""
    try:
        with conn.transaction():
            build()
    except psycopg.Error as error:
        assert error.sqlstate == code, f"wanted {code}, got {error.sqlstate}: {error}"
        assert error.diag.constraint_name == constraint, error.diag.constraint_name
        return
    raise AssertionError(f"wanted SQLSTATE {code} on {constraint}, the insert succeeded")


def marketplace_account(conn: Conn, tenant_id: UUID, **columns: Any) -> UUID:
    """An `active` Amazon account with a token, connected by a new owner of `tenant_id`."""
    n = _next()
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "marketplace": "amazon",
        "seller_id": f"SELLER{n}",
        "token_enc": b"\x01" * 48,
        "kms_key_version": "1",
    }
    if "connected_by_user_id" not in columns:
        defaults["connected_by_user_id"] = member(conn, tenant_id)
    return insert(conn, "marketplace_accounts", **{**defaults, **columns})  # type: ignore[no-any-return]


def image(conn: Conn, tenant_id: UUID, product_id: UUID, **columns: Any) -> UUID:
    """A `secondary` image of `product_id` with a fresh object key."""
    n = _next()
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "product_id": product_id,
        "storage_key": f"{tenant_id}/images/{n}.jpg",
        "slot": "secondary",
    }
    return insert(conn, "images", **{**defaults, **columns})  # type: ignore[no-any-return]


def batch(conn: Conn, tenant_id: UUID, **columns: Any) -> UUID:
    """An `approve` batch of one, created by a new owner of `tenant_id`."""
    defaults: dict[str, Any] = {"tenant_id": tenant_id, "kind": "approve", "item_count": 1}
    if "created_by_user_id" not in columns:
        defaults["created_by_user_id"] = member(conn, tenant_id)
    return insert(conn, "batches", **{**defaults, **columns})  # type: ignore[no-any-return]


def listing(conn: Conn, tenant_id: UUID, product_id: UUID | None = None, **columns: Any) -> UUID:
    """A `created` Amazon listing of `product_id` (a new standalone product by default)."""
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "product_id": product_id if product_id is not None else product(conn, tenant_id),
        "marketplace": "amazon",
        "category": "t_shirts",
    }
    return insert(conn, "listings", **{**defaults, **columns})  # type: ignore[no-any-return]


def version(conn: Conn, tenant_id: UUID, listing_id: UUID, **columns: Any) -> UUID:
    """A `generation` version of `listing_id`, generated by a new owner of `tenant_id`."""
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "listing_id": listing_id,
        "version": _next(),
        "content_json": {"title": "A title"},
        "origin": "generation",
    }
    if "generated_by_user_id" not in columns:
        defaults["generated_by_user_id"] = member(conn, tenant_id)
    return insert(conn, "listing_versions", **{**defaults, **columns})  # type: ignore[no-any-return]


def review_decision(
    conn: Conn, tenant_id: UUID, listing_id: UUID, version_id: UUID | None, **columns: Any
) -> UUID:
    """An `approve` decision on `version_id`, decided by a new reviewer of `tenant_id`."""
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "listing_id": listing_id,
        "listing_version_id": version_id,
        "decision": "approve",
    }
    if "decided_by_user_id" not in columns:
        defaults["decided_by_user_id"] = member(conn, tenant_id, role="reviewer")
    return insert(conn, "review_decisions", **{**defaults, **columns})  # type: ignore[no-any-return]


def listing_item(
    conn: Conn, tenant_id: UUID, listing_id: UUID, product_id: UUID, **columns: Any
) -> UUID:
    """A `standalone` Amazon item of `listing_id` for `product_id`; its seller SKU is the
    product's SKU (0004's li_guard: "seller_sku = products.sku")."""
    sku = conn.execute("SELECT sku FROM products WHERE id = %s", (product_id,)).fetchone()
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "listing_id": listing_id,
        "marketplace": "amazon",
        "product_id": product_id,
        "item_role": "standalone",
        "seller_sku": sku[0] if sku else f"ITEM-{_next()}",
    }
    return insert(conn, "listing_items", **{**defaults, **columns})  # type: ignore[no-any-return]


def validation_run(conn: Conn, tenant_id: UUID, listing_id: UUID, **columns: Any) -> UUID:
    """A passed first-attempt `schema` run on an Amazon listing."""
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "listing_id": listing_id,
        "marketplace": "amazon",
        "source": "schema",
        "passed": True,
        "error_report_json": {"errors": []},
        "attempt": 1,
    }
    return insert(conn, "validation_runs", **{**defaults, **columns})  # type: ignore[no-any-return]


def agent_run(conn: Conn, tenant_id: UUID, **columns: Any) -> UUID:
    """A completed `content` agent call (one row per model call, invariant 7)."""
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "trigger": "generate",
        "status": "completed",
        "model_used": "model-x",
        "request_id": "req-1",
        "agent_name": "content",
        "started_at": "2026-10-05T00:00:00Z",
    }
    return insert(conn, "agent_runs", **{**defaults, **columns})  # type: ignore[no-any-return]


def performance(conn: Conn, tenant_id: UUID, listing_id: UUID, **columns: Any) -> UUID:
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "listing_id": listing_id,
        "marketplace": "amazon",
        "date": "2026-10-01",
    }
    return insert(conn, "performance", **{**defaults, **columns})  # type: ignore[no-any-return]


def audit(conn: Conn, tenant_id: UUID | None, **columns: Any) -> UUID:
    """A `system` audit event of `tenant_id` (NULL: a platform event)."""
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "actor_type": "system",
        "action": "listing.approved",
        "entity_type": "listing",
    }
    return insert(conn, "audit_log", **{**defaults, **columns})  # type: ignore[no-any-return]


def brand_document(conn: Conn, tenant_id: UUID, **columns: Any) -> UUID:
    """An `uploaded` PDF brand document created by a new owner of `tenant_id`."""
    n = _next()
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "filename": f"guide-{n}.pdf",
        "storage_key": f"{tenant_id}/brand/{n}.pdf",
        "content_type": "application/pdf",
        "bytes": 1024,
        "content_hash": f"hash-{n}",
    }
    if "created_by" not in columns:
        defaults["created_by"] = member(conn, tenant_id)
    return insert(conn, "brand_documents", **{**defaults, **columns})  # type: ignore[no-any-return]


def embedding(value: float = 0.1, dims: int = 1536) -> str:
    """A pgvector text literal."""
    return "[" + ",".join([str(value)] * dims) + "]"


def brand_chunk(conn: Conn, tenant_id: UUID, **columns: Any) -> UUID:
    """An active `tone_rule` chunk of a brand document; `embedding` is a vector literal."""
    n = _next()
    values: dict[str, Any] = {
        "tenant_id": tenant_id,
        "type": "tone_rule",
        "text": f"Chunk {n}",
        "embedding": embedding(),
        "embedding_model": "text-embedding-3-small",
        "content_hash": f"chunk-{n}",
        "source_type": "brand_document",
        "source_id": UUID(int=n),
        "chunk_index": 0,
        **columns,
    }
    names = list(values)
    query = sql.SQL("INSERT INTO brand_chunks ({}) VALUES ({}) RETURNING id").format(
        sql.SQL(", ").join(sql.Identifier(name) for name in names),
        sql.SQL(", ").join(
            sql.SQL("%s::vector") if name == "embedding" else sql.Placeholder() for name in names
        ),
    )
    row = conn.execute(query, [_adapt(values[name]) for name in names]).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def human_task(conn: Conn, tenant_id: UUID, **columns: Any) -> UUID:
    """An open `missing_fact` question on `facts.fabric` of a new product."""
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "kind": "missing_fact",
        "target_path": "facts.fabric",
        "question": "What is the fabric?",
        "reason": "Amazon requires the fabric.",
        "expected_answer": {"kind": "text", "max_length": 100},
        "resume_from": "generate",
    }
    if "product_id" not in columns:
        defaults["product_id"] = product(conn, tenant_id)
    return insert(conn, "human_tasks", **{**defaults, **columns})  # type: ignore[no-any-return]


def listing_issue(conn: Conn, tenant_id: UUID, listing_id: UUID, **columns: Any) -> UUID:
    """An open `rules_engine` error on `content.title` of an Amazon listing."""
    values: dict[str, Any] = {
        "tenant_id": tenant_id,
        "listing_id": listing_id,
        "marketplace": "amazon",
        "source": "rules_engine",
        "field_path": "content.title",
        "error_code": "FIELD_TOO_LONG",
        "error_class": "content_error",
        "retry_class": "ai_correct",
        "severity": "error",
        "message": "The title is too long.",
        **columns,
    }
    if "dedupe_key" not in values:
        prefix = f"{values['source']}|{values['field_path']}|{values['error_code']}|"
        values["dedupe_key"] = prefix + str(values.get("rule_id") or "")
    return insert(conn, "listing_issues", **values)  # type: ignore[no-any-return]
