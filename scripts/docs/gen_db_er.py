#!/usr/bin/env python3
"""Generate the database sections of docs/handover/DATABASE.md from the real schema.

Usage (from the repo root):

    .venv/bin/python scripts/docs/gen_db_er.py           # rewrite the generated block
    .venv/bin/python scripts/docs/gen_db_er.py --check   # exit 1 if the file is out of date

Two sources, and no database connection:

1. The SQLAlchemy metadata (`app.db.base.metadata`, filled by importing `app.db.models`): tables,
   columns, types, defaults, primary keys, foreign keys, unique constraints and indexes. The
   models mirror the migrations; CI's `alembic check` keeps the two equal.
2. The migrations rendered offline (`alembic upgrade head --sql`, which only prints SQL): CHECK
   constraints (they live only in the migrations), row-level security, policies, triggers and
   views. The script runs Alembic in a subprocess with placeholder settings; offline mode never
   opens a connection.

Everything between `<!-- BEGIN GENERATED -->` and `<!-- END GENERATED -->` is replaced. The
hand-written text outside the markers is kept as it is. Output is deterministic: every list is
sorted and nothing depends on time or on the machine.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
API_DIR = REPO / "apps" / "api"
OUT = REPO / "docs" / "handover" / "DATABASE.md"
BEGIN = "<!-- BEGIN GENERATED -->"
END = "<!-- END GENERATED -->"

# `make docs-handover` sets harmless local placeholders (APP_ENV, APP_ROLE, ALEMBIC_DATABASE_URL):
# the models import no settings, but Alembic's env.py does, and offline mode needs a URL only to
# pick the dialect. Nothing there is a real credential, and nothing connects.
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

import app.db.models  # noqa: E402,F401  - registers every table on the shared MetaData
from app.db.base import Base, metadata  # noqa: E402
from sqlalchemy import Column, ForeignKeyConstraint, Table, UniqueConstraint  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.sql.schema import CallableColumnDefault, Computed, FetchedValue  # noqa: E402

PG = postgresql.dialect()


def require(condition: object, message: str) -> None:
    """Stop the run with `message` when `condition` is false (asserts vanish under -O)."""
    if not condition:
        sys.exit(f"gen_db_er: {message}")


# --------------------------------------------------------------------------------------------
# Hand-maintained wording: domain titles and one-line table purposes. The schema facts below
# them are all read from the code. A table missing here fails the run (see `check_coverage`).
# --------------------------------------------------------------------------------------------

DOMAINS: dict[str, tuple[str, str]] = {
    # module: (title, one-paragraph summary)
    "tenancy": (
        "Tenancy and identity",
        "Who the brands (tenants) are, who the people are, and which role each person has in "
        "each brand. A person has one global login (`users`) and one membership per brand.",
    ),
    "catalog": (
        "Catalog: products and variants",
        "The seller's products. A product is a standalone item, a style (the family parent) or "
        "a variant (one size or colour of a style). Colourways and the size x colour matrix "
        "describe which variants of a style exist.",
    ),
    "catalog_assets": (
        "Catalog assets: images, marketplace accounts, batches",
        "Product images (the file itself is in object storage), the brand's connected "
        "marketplace seller accounts, and batches that group a bulk action.",
    ),
    "facts": (
        "Fact owners: brand profile and business parties",
        "The brand-level facts (one profile per tenant) and the address book of legal parties "
        "(manufacturer, packer, importer, consumer care) that products point to.",
    ),
    "listings": (
        "Listings, versions, reviews and items",
        "A listing is one product on one marketplace. Its content lives in immutable versions; "
        "reviews approve or reject an exact version; items are the sellable SKUs inside it.",
    ),
    "rules": (
        "Rules (Rules Engine)",
        "The Rules Engine's rules: platform rules (no tenant) and a brand's own rules.",
    ),
    "runs": (
        "Runs: validation, AI calls, performance",
        "Append-only records of each validation pass and each AI model call, and the daily "
        "marketplace performance numbers per listing.",
    ),
    "issues": (
        "Issues and human tasks",
        "Problems found on a listing (by validation or by the marketplace) and the questions the "
        "system asks a person when it cannot fix a problem alone.",
    ),
    "ops": (
        "Operations: jobs, imports, auth, publishing ledger",
        "Background job runs, CSV imports, login and OAuth tokens, idempotency keys, rate-limit "
        "counters, and the ledger of every submission sent to a marketplace.",
    ),
    "audit_brand": (
        "Audit trail and brand voice",
        "The append-only audit log, and the brand documents whose text is cut into chunks and "
        "embedded as vectors for brand-voice search.",
    ),
    "platform": (
        "Platform-global reference data",
        "Data shared by every tenant and changed only by migrations or operator commands: "
        "marketplace category templates, the marketplace error map, the attribute dictionary "
        "and the listing status transition table. No `tenant_id`, no RLS, read-only for the app.",
    ),
}

PURPOSE: dict[str, str] = {
    "tenants": "One row per brand (tenant). Holds the brand's lifecycle status (trial, active, "
    "suspended, offboarded), plan, brand voice basics and the purge dates. Every tenant row in "
    "every other table points here.",
    "users": "One global login per person (email + password hash). Not tenant-scoped: a person "
    "reaches a brand only through a membership.",
    "tenant_memberships": "A person's role in one brand (owner, operator, reviewer, admin). "
    "Actor columns elsewhere (who generated, approved, decided) point here, so an actor is "
    "always a member of the same tenant.",
    "invitations": "A pending invite of an email address to a brand with a role. Only the sha256 "
    "of the invite token is stored.",
    "products": "A seller product: a standalone product, a style (family parent, has a "
    "`variation_theme`) or a variant (has `parent_product_id`). Also holds the typed product "
    "facts (price, tax, origin, legal parties, identifiers, dimensions) and their provenance.",
    "product_colourways": "One colour of a style. Variants, images and matrix cells of that colour "
    "point to it.",
    "variant_matrix_cells": "One cell of a style's size x colour grid: is that combination "
    "available, and which variant product sells it. A trigger keeps the matrix complete.",
    "images": "One product image. The bytes are in object storage; the row keeps the object key, "
    "slot, compliance status and the image check report.",
    "marketplace_accounts": "A brand's connected seller account on Amazon or Flipkart. The OAuth "
    "token is stored envelope-encrypted (`token_enc`) and is decrypted only inside a marketplace "
    "adapter call.",
    "batches": "Groups the items of one bulk action (generate, regenerate, approve, reject, "
    "publish, status refresh) so results can be reported together.",
    "brand_profiles": "The brand-level facts, one row per tenant (`tenant_id` is the primary "
    "key): brand names, owner type, default legal parties, default country of origin, locale.",
    "business_parties": "The tenant's address book of legal entities (manufacturer, marketer, "
    "packer, importer, consumer care). Contact columns are personal data and are hidden from "
    "the read-only roles.",
    "listings": "One product on one marketplace. Holds the workflow status, pointers to the "
    "current / approved / published / live versions, and the marketplace-side state and sync "
    "schedule.",
    "listing_versions": "An immutable snapshot of a listing's content (`content_json`). A new "
    "version is written for every generation, regeneration and human edit. The only allowed "
    "change is the one-time approval stamp (enforced by the `lv_guard` trigger).",
    "review_decisions": "One review decision (approve, reject, regenerate request) on an exact "
    "listing version, with the reviewer and the facts digest at that moment. Append-only.",
    "listing_items": "One sellable marketplace SKU inside a listing: the standalone product, the "
    "family parent, or one child variant. Tracks per-SKU marketplace state.",
    "rules": "A Rules Engine rule. `tenant_id` NULL means a platform rule that every brand "
    "sees; otherwise it is the brand's own rule. A rule's content never changes: new content is "
    "a new row with a higher `version`. Only `enabled` may be switched on an existing row, and "
    "each switch takes a new `revision` number (the `rules_guard` trigger enforces both).",
    "validation_runs": "One validation pass over one listing version: one checker (`source`, for "
    "example `rules_engine` or `schema`) on one generation `attempt`. Findings are stored in the "
    "shared ErrorReport shape. Append-only.",
    "agent_runs": "One AI model call made for a product or listing: model, tokens, cost, timing, "
    "status. Written only by the backend worker. Append-only.",
    "performance": "Daily marketplace numbers for a listing (impressions, clicks, orders, "
    "returns, revenue).",
    "human_tasks": "A question the system asks a person (for example a missing fact). It moves "
    "open -> answered -> applied (or cancelled), and the answer resumes the paused work.",
    "listing_issues": "One problem on a listing, found by validation or returned by the "
    "marketplace, normalised to an error code, class and fix action. At most one open issue per "
    "listing and dedupe key.",
    "job_runs": "The source of truth for every background job (generate, publish, status sync, "
    "import, image check, brand embed): status, attempts, lease, result. The queue entry only "
    "carries this row's id.",
    "imports": "One uploaded CSV file and its progress counters. The file is in object storage.",
    "import_rows": "One row of an import file, its parse status and the product it created or "
    "updated.",
    "idempotency_keys": "Stored responses for requests sent with an `Idempotency-Key` header, so "
    "a retried request returns the first response instead of acting twice.",
    "refresh_tokens": "Login refresh tokens (sha256 only), with rotation family and revocation. "
    "The source of truth for logout.",
    "oauth_states": "Short-lived state of a marketplace OAuth connect flow (state hash, PKCE "
    "verifier) and, between callback and confirm, the sealed token pair.",
    "rate_limit_counters": "Per-minute request counters for the rate limiter. No tenant, no RLS, "
    "UNLOGGED (emptied after a crash, which is fine for counters).",
    "listing_submissions": "The write-ahead ledger of every submission sent to a marketplace: one "
    "row per attempt, written before the call, then updated with the outcome. Payload and raw "
    "response are in object storage, referenced by key and sha256.",
    "publish_field_results": "Per-field result of a submission for each listing item (child "
    "SKU). Append-only.",
    "audit_log": "The append-only audit trail: who did what to which entity, with an allow-listed "
    "before/after. `tenant_id` is NULL only for platform events.",
    "brand_documents": "An uploaded brand guideline document. The file is in object storage; "
    "the row tracks its processing status and chunk count.",
    "brand_chunks": "A chunk of brand text with its 1536-dimension embedding (pgvector), used to "
    "retrieve brand voice for generation. The text is untrusted prompt data.",
    "category_templates": "A marketplace category's attribute template, versioned. At most one "
    "version per marketplace and category is current (`ct_one_current`); validation runs pin the "
    "version they used.",
    "marketplace_error_map": "Maps a raw marketplace error to our error code, class, retry class "
    "and the plain-language message and seller action.",
    "attribute_definitions": "The attribute dictionary: every product fact key, where it is "
    "stored, who may own it, its type and which sources may set it.",
    "attribute_columns": "Maps typed fact columns (for example `products.mrp`) to their "
    "dictionary key.",
    "category_attributes": "Which dictionary attributes matter for a category, and whether the "
    "category makes one critical.",
    "listing_status_transitions": "The allowed listing status moves: (from, to, event) plus who "
    "may make them. The status guard trigger reads this table on every status change.",
}

# Columns shown in the overview diagrams besides keys and tenant_id.
STATUSY = re.compile(
    r"(^|_)(status|state|kind|role|decision|outcome|origin|availability|severity|"
    r"item_role|actor_type|marketplace)$|^is_|^enabled$|^deleted_at$|^archived_at$|^paused_at$"
)

# Tables drawn in the cross-domain picture (hub tables), in no particular order.
HUBS = (
    "tenants", "users", "tenant_memberships",
    "products", "product_colourways", "images",
    "brand_profiles", "business_parties", "marketplace_accounts", "batches",
    "listings", "listing_versions", "review_decisions", "listing_items",
    "listing_submissions", "validation_runs", "listing_issues", "human_tasks",
    "job_runs", "rules", "category_templates",
    "brand_documents", "brand_chunks", "audit_log",
)  # fmt: skip

# Model comments that describe build history rather than the schema; the text shown instead.
NOTE_OVERRIDE: dict[tuple[str, str], str] = {
    ("batches", "import_id"): "soft reference to `imports.id` (no foreign key); set only on a "
    "generate batch",
    ("product_colourways", "uq_product_colourways_id_style_product_id"): "the target of the "
    "same-style composite foreign keys from variant_matrix_cells and images",
    ("brand_chunks", "brand_chunks_embedding_idx"): "cosine HNSW index for brand-voice search, "
    "built CONCURRENTLY in revision 0002b",
}

# --------------------------------------------------------------------------------------------
# Offline migration SQL: CHECKs, RLS, policies, triggers, views.
# --------------------------------------------------------------------------------------------


def render_migrations_sql() -> str:
    """`alembic upgrade head --sql`: offline mode prints the SQL and never connects."""
    with tempfile.TemporaryDirectory() as tmp:  # no stray .env is read from the cwd
        proc = subprocess.run(  # noqa: S603 - fixed argv: this interpreter running alembic
            [sys.executable, "-m", "alembic", "-c", str(API_DIR / "alembic.ini"),
             "upgrade", "head", "--sql"],
            cwd=tmp, capture_output=True, text=True, check=False,
        )  # fmt: skip
    if proc.returncode != 0:
        sys.exit(f"alembic --sql failed:\n{proc.stderr[-2000:]}")
    return proc.stdout


def strip_sql_comments(sql: str) -> str:
    """Drop `-- ...` comments that are outside quotes and dollar-quoted bodies."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" and j + 1 < n and sql[j + 1] == "'":
                    j += 2
                    continue
                if sql[j] == "'":
                    break
                j += 1
            out.append(sql[i : j + 1])
            i = j + 1
        elif c == "$":
            m = re.match(r"\$[A-Za-z_]*\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                j = n if j < 0 else j + len(tag)
                out.append(sql[i:j])
                i = j
            else:
                out.append(c)
                i += 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def split_top_level(text: str, sep: str) -> list[str]:
    """Split on `sep` outside quotes, dollar quotes and parentheses."""
    parts: list[str] = []
    depth, start, i, n = 0, 0, 0, len(text)
    while i < n:
        c = text[i]
        if c == "'":
            j = i + 1
            while j < n and not (text[j] == "'" and (j + 1 >= n or text[j + 1] != "'")):
                j += 2 if text[j] == "'" else 1
            i = j + 1
            continue
        if c == "$":
            m = re.match(r"\$[A-Za-z_]*\$", text[i:])
            if m:
                j = text.find(m.group(0), i + len(m.group(0)))
                i = n if j < 0 else j + len(m.group(0))
                continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class SqlFacts:
    checks: dict[str, dict[str, str]] = field(default_factory=lambda: defaultdict(dict))
    rls_enabled: set[str] = field(default_factory=set)
    rls_forced: set[str] = field(default_factory=set)
    policies: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    triggers: dict[str, dict[str, tuple[str, str]]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    views: set[str] = field(default_factory=set)
    invoker_views: set[str] = field(default_factory=set)
    transitions: list[tuple[str, str, str, str]] = field(default_factory=list)
    tables: set[str] = field(default_factory=set)
    unlogged: set[str] = field(default_factory=set)


TBL = r"(?:ONLY\s+)?(?:public\.)?(\w+)"


def parse_sql(sql: str) -> SqlFacts:
    f = SqlFacts()
    for stmt in split_top_level(strip_sql_comments(sql), ";"):
        s = ws(stmt)
        m = re.match(r"CREATE (UNLOGGED )?TABLE (?:IF NOT EXISTS )?(?:public\.)?(\w+) ?\(", s)
        if m:
            table = m.group(2)
            f.tables.add(table)
            if m.group(1):
                f.unlogged.add(table)
            body = s[m.end() - 1 :]
            depth = 0
            for k, ch in enumerate(body):  # the column list ends at the matching paren
                depth += ch == "("
                depth -= ch == ")"
                if depth == 0:
                    body = body[1:k]
                    break
            for element in split_top_level(body, ","):
                c = re.match(r"CONSTRAINT (\w+) CHECK \((.*)\)(?: NOT VALID)?$", element)
                if c:
                    f.checks[table][c.group(1)] = c.group(2)
            continue
        m = re.match(rf"ALTER TABLE (?:IF EXISTS )?{TBL} (.*)$", s)
        if m:
            table, rest = m.group(1), m.group(2)
            for action in split_top_level(rest, ","):
                a = re.match(r"ADD CONSTRAINT (\w+) CHECK \((.*)\)(?: NOT VALID)?$", action)
                if a:
                    f.checks[table][a.group(1)] = a.group(2)
                d = re.match(r"DROP CONSTRAINT (?:IF EXISTS )?(\w+)", action)
                if d:
                    f.checks[table].pop(d.group(1), None)
                if action == "ENABLE ROW LEVEL SECURITY":
                    f.rls_enabled.add(table)
                elif action == "DISABLE ROW LEVEL SECURITY":
                    f.rls_enabled.discard(table)
                elif action == "FORCE ROW LEVEL SECURITY":
                    f.rls_forced.add(table)
                elif action == "NO FORCE ROW LEVEL SECURITY":
                    f.rls_forced.discard(table)
            continue
        m = re.match(rf"CREATE POLICY (\w+) ON {TBL}", s)
        if m:
            f.policies[m.group(2)].add(m.group(1))
            continue
        m = re.match(rf"DROP POLICY (?:IF EXISTS )?(\w+) ON {TBL}", s)
        if m:
            f.policies[m.group(2)].discard(m.group(1))
            continue
        m = re.match(
            rf"CREATE (?:OR REPLACE )?(?:CONSTRAINT )?TRIGGER (\w+) (.*?) ON {TBL} .*"
            r"EXECUTE (?:FUNCTION|PROCEDURE) (?:public\.)?(\w+)",
            s,
        )
        if m:
            when = m.group(2)
            if "DEFERRABLE INITIALLY DEFERRED" in s:
                when += ", checked at commit"
            f.triggers[m.group(3)][m.group(1)] = (when, m.group(4))
            continue
        m = re.match(rf"DROP TRIGGER (?:IF EXISTS )?(\w+) ON {TBL}", s)
        if m:
            f.triggers[m.group(2)].pop(m.group(1), None)
            continue
        m = re.match(
            r"INSERT INTO (?:public\.)?listing_status_transitions \(([^)]*)\) VALUES (.*)$", s
        )
        if m:
            cols = [c.strip() for c in m.group(1).split(",")]
            for row in split_top_level(m.group(2), ","):
                vals = [v.strip().strip("'") for v in split_top_level(row.strip()[1:-1], ",")]
                r = dict(zip(cols, vals, strict=True))
                f.transitions.append((r["from_status"], r["to_status"], r["event"], r["actor"]))
            continue
        m = re.match(r"CREATE (?:OR REPLACE )?VIEW (?:public\.)?(\w+)", s)
        if m:
            f.views.add(m.group(1))
            if re.match(r"CREATE (?:OR REPLACE )?VIEW \S+ WITH \([^)]*security_invoker", s):
                f.invoker_views.add(m.group(1))
            continue
        m = re.match(r"DROP VIEW (?:IF EXISTS )?(?:public\.)?(\w+)", s)
        if m:
            f.views.discard(m.group(1))
    return f


def allowed_values(expr: str) -> tuple[str, list[str]] | None:
    """`col IN ('a', 'b')` (optionally `col IS NULL OR ...`) -> (col, [a, b])."""
    e = ws(expr)
    while e.startswith("(") and e.endswith(")") and split_top_level(e[1:-1], "\0") == [e[1:-1]]:
        inner = e[1:-1]
        depth, ok = 0, True
        for ch in inner:
            depth += ch == "("
            depth -= ch == ")"
            if depth < 0:
                ok = False
                break
        if not ok:
            break
        e = inner.strip()
    m = re.match(r"^(?:(\w+) IS NULL OR )?(\w+) IN \(((?:\s*'[^']*'\s*,?)+)\)$", e)
    if not m or (m.group(1) and m.group(1) != m.group(2)):
        return None
    return m.group(2), re.findall(r"'([^']*)'", m.group(3))


# --------------------------------------------------------------------------------------------
# Comments in the model source: a `# ...` line above a column or a __table_args__ entry.
# --------------------------------------------------------------------------------------------

_REF = r"(?:DATABASE\.md|BACKEND-ARCHITECTURE|SUPERSEDED\.md|CANON|A§|KI-[A-Z]|PLAN|§|\b[TSD]\d+[a-z]?\b)"


def trigger_when(when: str) -> str:
    """`BEFORE INSERT OR UPDATE OF a, b, ... FOR EACH ROW` -> short lower-case text."""
    w = ws(when).lower()
    m = re.match(r"(.*? of )(.*)$", w)
    if m:
        cols = [c.strip() for c in m.group(2).split(",")]
        if len(cols) > 4:
            w = f"{m.group(1)}{', '.join(cols[:3])} and {len(cols) - 3} more columns"
    return w


# Implementation remarks about the ORM or Alembic tell an API reader nothing; they are dropped.
_IMPL = re.compile(
    r"alembic|FetchedValue|eager_defaults|deparse|SQLAlchemy|_drop_caller|use_alter|"
    r"Written as|RETURNING load|arrives in \d{4}",
    re.IGNORECASE,
)


def clean_comment(text: str) -> str:
    """Drop internal planning references so the note reads on its own."""
    t = ws(text)
    t = re.sub(r"\b(\d{4}[a-z]?) \((?:S\d+[a-z]?)\):\s*", r"added in revision \1: ", t)
    t = re.sub(rf"\s*\([^()]*{_REF}[^()]*\)", "", t)
    t = re.sub(r"^(?:[TDS]\d+[a-z]?|KI-[A-Z])\s*:\s*", "", t)
    t = re.sub(rf",?\s*{_REF}\S*", "", t) if re.search(r"DATABASE\.md|SUPERSEDED", t) else t
    t = t.replace("`", "")
    t = " ".join(
        p
        for p in re.split(r"(?<=[.;])\s+|,\s+(?=FetchedValue|and _drop)", t)
        if not _IMPL.search(p)
    )
    t = re.sub(r"\s*\((?:invariant \d+|\d{4}[a-z]?)\)", "", t)
    t = t.replace("→ l/mkt", "(listing_id, marketplace) foreign key")
    t = t.replace("→ lv", "(listing_id, version id) foreign key").replace("→", "->")
    return t.strip(" ;,:")


def source_comments() -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    """(table, column) -> comment and (table, constraint name) -> comment."""
    col_notes: dict[tuple[str, str], str] = {}
    con_notes: dict[tuple[str, str], str] = {}
    cls_by_name = {m.class_.__name__: m for m in Base.registry.mappers}
    for module in sorted({m.class_.__module__ for m in Base.registry.mappers}):
        path = Path(importlib.import_module(module).__file__ or "")
        lines = path.read_text(encoding="utf-8").splitlines()

        def above(lineno: int, lines: list[str] = lines) -> str:
            got: list[str] = []
            k = lineno - 2
            while k >= 0 and lines[k].strip().startswith("#"):
                got.insert(0, lines[k].strip().lstrip("#").strip())
                k -= 1
            same = lines[lineno - 1].split("  # ", 1)
            if len(same) == 2 and "noqa" not in same[1] and "fmt:" not in same[1]:
                got.append(same[1].strip())
            return " ".join(got)

        tree = ast.parse("\n".join(lines))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or node.name not in cls_by_name:
                continue
            mapper = cls_by_name[node.name]
            table = mapper.local_table.name
            keys = {a.key: a.columns[0].name for a in mapper.column_attrs}
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    name = keys.get(item.target.id)
                    note = above(item.lineno)
                    if name and note:
                        col_notes[(table, name)] = NOTE_OVERRIDE.get(
                            (table, name), clean_comment(note)
                        )
                if (
                    isinstance(item, ast.Assign)
                    and any(
                        isinstance(t, ast.Name) and t.id == "__table_args__" for t in item.targets
                    )
                    and isinstance(item.value, ast.Tuple)
                ):
                    for el in item.value.elts:
                        note = above(el.lineno)
                        cname = _constraint_name(el)
                        if note and cname:
                            con_notes[(table, cname)] = NOTE_OVERRIDE.get(
                                (table, cname), clean_comment(note)
                            )
    return col_notes, con_notes


def _constraint_name(node: ast.AST) -> str | None:
    """The `conv("name")` inside a constraint or index call, if any."""
    if not isinstance(node, ast.Call):
        return None
    candidates = [kw.value for kw in node.keywords if kw.arg == "name"] + list(node.args[:1])
    for c in candidates:
        if (
            isinstance(c, ast.Call)
            and getattr(c.func, "id", "") == "conv"
            and c.args
            and isinstance(c.args[0], ast.Constant)
        ):
            return str(c.args[0].value)
        if isinstance(c, ast.Constant) and isinstance(c.value, str):
            return c.value
    return None


# --------------------------------------------------------------------------------------------
# Metadata helpers
# --------------------------------------------------------------------------------------------


def module_of() -> dict[str, str]:
    return {
        m.local_table.name: m.class_.__module__.rsplit(".", 1)[-1] for m in Base.registry.mappers
    }


def docstring_of() -> dict[str, str]:
    out = {}
    for m in Base.registry.mappers:
        doc = m.class_.__doc__
        if doc and m.class_.__doc__ != Base.__doc__:
            out[m.local_table.name] = clean_comment(doc)
    return out


def pg_type(col: Column) -> str:
    t = col.type.compile(dialect=PG).lower()
    t = t.replace("timestamp with time zone", "timestamptz")
    return t.replace(", ", ",")


def er_type(col: Column) -> str:
    """A single token for a Mermaid attribute type."""
    t = pg_type(col)
    if t.endswith("[]"):
        return t[:-2].split("(")[0] + "_array"
    return re.sub(r"[^a-z0-9_]", "", t.split("(")[0]) or "value"


def default_of(col: Column) -> str:
    if isinstance(col.computed, Computed) or col.computed is not None:
        return "generated (stored)"
    if col.server_default is not None:
        if isinstance(col.server_default, FetchedValue) and not hasattr(col.server_default, "arg"):
            return "set by trigger"
        arg = getattr(col.server_default, "arg", None)
        text = getattr(arg, "text", None) or str(arg)
        return f"`{ws(text)}`"
    if isinstance(col.default, CallableColumnDefault):
        return "set by the app on insert"
    if col.default is not None and getattr(col.default, "arg", None) is not None:
        return f"`{col.default.arg!r}`"
    return ""


def fk_parent_cols(fk: ForeignKeyConstraint) -> list[str]:
    return [e.column.name for e in fk.elements]


def is_std_tenant_fk(fk: ForeignKeyConstraint) -> bool:
    return [c.name for c in fk.columns] == ["tenant_id"] and fk.referred_table.name == "tenants"


def own_cols(fk: ForeignKeyConstraint) -> list[str]:
    """The FK column(s) that name the parent row: the ones referencing the parent's primary key.
    The other columns of a composite FK (`tenant_id`, `listing_id`, `marketplace`) only pin the
    parent to the same tenant, listing or marketplace, so a label leaves them out."""
    pk = {c.name for c in fk.referred_table.primary_key.columns}
    pairs = list(zip([c.name for c in fk.columns], fk_parent_cols(fk), strict=True))
    naming = [c for c, p in pairs if p in pk and c != "tenant_id"]
    rest = [c for c, _ in pairs if c != "tenant_id"]
    return naming or rest or [c for c, _ in pairs]


def fk_optional(fk: ForeignKeyConstraint) -> bool:
    return any(c.nullable for c in fk.columns)


def child_unique(table: Table, fk: ForeignKeyConstraint) -> bool:
    cols = {c.name for c in fk.columns}
    keys = [{c.name for c in table.primary_key.columns}]
    keys += [
        {c.name for c in u.columns} for u in table.constraints if isinstance(u, UniqueConstraint)
    ]
    keys += [
        {c.name for c in i.columns}
        for i in table.indexes
        if i.unique and i.columns and i.dialect_options["postgresql"].get("where") is None
    ]
    return any(k and k <= cols for k in keys)


def fks(table: Table) -> list[ForeignKeyConstraint]:
    return sorted(
        (c for c in table.constraints if isinstance(c, ForeignKeyConstraint)),
        key=lambda c: (c.referred_table.name, [x.name for x in c.columns], c.name or ""),
    )


def uniques(table: Table) -> list[UniqueConstraint]:
    return sorted(
        (c for c in table.constraints if isinstance(c, UniqueConstraint)),
        key=lambda c: c.name or "",
    )


def sql_text(clause, table: str) -> str:
    """A SQL expression as PostgreSQL text, bound values inlined, table prefix dropped."""
    if isinstance(clause, Column):
        return clause.name
    if isinstance(clause, str):
        return ws(clause)
    try:
        s = str(clause.compile(dialect=PG, compile_kwargs={"literal_binds": True}))
    except Exception:  # a clause that cannot inline its binds prints as is
        s = str(clause)
    return re.sub(rf"\b{table}\.", "", ws(s))


def index_cols(index) -> str:
    ops = index.dialect_options["postgresql"].get("ops") or {}
    parts = []
    for e in index.expressions:
        s = sql_text(e, index.table.name)
        if s in ops:
            s += f" {ops[s]}"
        parts.append(s)
    return ", ".join(parts)


# --------------------------------------------------------------------------------------------
# Mermaid
# --------------------------------------------------------------------------------------------

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def edge(parent: str, child: str, optional: bool, one_to_one: bool, label: str) -> str:
    left = "|o" if optional else "||"
    right = "o|" if one_to_one else "o{"
    return f'    {parent} {left}--{right} {child} : "{label}"'


def grouped_edges(pairs: list[tuple[str, str, bool, bool, str]]) -> list[str]:
    """Merge FKs that share (parent, child, cardinality) into one line with joined labels."""
    groups: dict[tuple[str, str, bool, bool], list[str]] = defaultdict(list)
    for parent, child, opt, one, label in pairs:
        groups[(parent, child, opt, one)].append(label)
    lines = []
    for (parent, child, opt, one), labels in sorted(groups.items()):
        uniq = sorted(set(labels))
        text = (
            ", ".join(uniq) if len(uniq) <= 4 else ", ".join(uniq[:3]) + f" +{len(uniq) - 3} more"
        )
        lines.append(edge(parent, child, opt, one, text))
    return lines


def attr_line(table: Table, col: Column, fk_cols: set[str], uk_cols: set[str]) -> str:
    keys = []
    if col.primary_key:
        keys.append("PK")
    if col.name in fk_cols:
        keys.append("FK")
    if col.name in uk_cols and not col.primary_key:
        keys.append("UK")
    key = (" " + ", ".join(keys)) if keys else ""
    note = ' "nullable"' if col.nullable and not col.primary_key else ""
    return f"        {er_type(col)} {col.name}{key}{note}"


def domain_diagram(module: str, tables: list[Table]) -> tuple[str, int, int]:
    names = {t.name for t in tables}
    lines = ["```mermaid", "erDiagram"]
    pairs = []
    external: set[str] = set()
    for t in tables:
        fk_cols = {c for fk in fks(t) for c in own_cols(fk)}
        uk_cols = {u.columns.keys()[0] for u in uniques(t) if len(u.columns) == 1}
        shown = [
            c for c in t.columns
            if c.primary_key or c.name in fk_cols or c.name == "tenant_id" or c.name in uk_cols
            or STATUSY.search(c.name)
        ]  # fmt: skip
        lines.append(f"    {t.name} {{")
        lines += [attr_line(t, c, fk_cols, uk_cols) for c in shown]
        lines.append("    }")
        for fk in fks(t):
            parent = fk.referred_table.name
            if is_std_tenant_fk(fk) and not (
                module == "tenancy" or fk_optional(fk) or child_unique(t, fk)
            ):
                continue  # the line every tenant table has; kept when it says something more
            if parent not in names:
                external.add(parent)
            pairs.append(
                (parent, t.name, fk_optional(fk), child_unique(t, fk), ", ".join(own_cols(fk)))
            )
    rel = grouped_edges(pairs)
    lines += rel
    lines.append("```")
    for n in names | external:
        require(IDENT.match(n), f"not a valid Mermaid entity name: {n}")
    return "\n".join(lines), len(names | external), len(rel)


def domain_map(tables: dict[str, Table], mod: dict[str, str]) -> str:
    """A flowchart of the domains, with one arrow per (child domain, parent domain)."""
    counts: dict[tuple[str, str], int] = defaultdict(int)
    to_tenancy: set[str] = set()
    for name, t in tables.items():
        for fk in fks(t):
            a, b = mod[name], mod[fk.referred_table.name]
            if a == b:
                continue
            if b == "tenancy":
                to_tenancy.add(a)
                continue
            counts[(a, b)] += 1
    order = DOMAIN_ORDER.index
    lines = ["```mermaid", "flowchart LR"]
    for m in sorted(set(mod.values()), key=order):
        lines.append(f'    {m}["{DOMAINS[m][0]}"]')
    for (a, b), n in sorted(counts.items(), key=lambda kv: (order(kv[0][0]), order(kv[0][1]))):
        lines.append(f'    {a} -->|"{n} FK"| {b}')
    for a in sorted(to_tenancy, key=order):
        lines.append(f"    {a} -.-> tenancy")
    lines.append("```")
    return "\n".join(lines)


def big_picture(tables: dict[str, Table]) -> tuple[str, int]:
    hubs = [h for h in HUBS if h in tables]
    hubset = set(hubs)
    pairs = []
    connected: set[str] = set()
    tenant_edges = []
    for h in sorted(hubs):
        t = tables[h]
        for fk in fks(t):
            parent = fk.referred_table.name
            if parent not in hubset or parent == h:
                continue
            if parent == "tenant_memberships" and h not in {"invitations"}:
                continue  # actor columns; drawn in the domain diagrams
            item = (parent, h, fk_optional(fk), child_unique(t, fk), ", ".join(own_cols(fk)))
            if is_std_tenant_fk(fk):
                tenant_edges.append(item)
                continue
            pairs.append(item)
            connected |= {parent, h}
    for item in tenant_edges:  # a tenant edge only where the table has no other hub edge
        if item[1] not in connected:
            pairs.append(item)
            connected |= {item[0], item[1]}
    # one line per pair in the picture: the label lists the FK columns
    merged: dict[tuple[str, str], list] = {}
    for parent, child, opt, one, label in pairs:
        cur = merged.setdefault((parent, child), [True, True, []])
        cur[0] &= opt
        cur[1] &= one
        cur[2].append(label)
    lines = ["```mermaid", "erDiagram"]
    for (parent, child), (opt, one, labels) in sorted(merged.items()):
        uniq = sorted(set(labels))
        text = ", ".join(uniq) if len(uniq) <= 2 else f"{uniq[0]} +{len(uniq) - 1} more"
        lines.append(edge(parent, child, opt, one, text))
    lines.append("```")
    missing = hubset - connected
    require(not missing, f"hub tables with no edge in the big picture: {sorted(missing)}")
    require(len(hubs) <= 30, "the big picture allows at most 30 tables")
    return "\n".join(lines), len(hubs)


# --------------------------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------------------------


def md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def anchor(name: str) -> str:
    """GitHub's heading id for a heading that is just `name` in backticks."""
    return re.sub(r"[^a-z0-9_-]", "", name.lower())


def generate() -> str:
    sql = parse_sql(render_migrations_sql())
    col_notes, con_notes = source_comments()
    mod = module_of()
    docs = docstring_of()
    tables = {t.name: t for t in metadata.sorted_tables}
    by_module: dict[str, list[Table]] = defaultdict(list)
    for t in metadata.sorted_tables:
        by_module[mod[t.name]].append(t)
    for m in by_module:
        by_module[m].sort(key=lambda t: t.name)

    not_created = set(tables) - sql.tables
    require(
        not not_created, f"model tables no migration creates (parse error?): {sorted(not_created)}"
    )
    for name, t in tables.items():
        if "tenant_id" in t.c:
            require(name in sql.rls_enabled and name in sql.rls_forced, f"{name}: RLS not forced")
    unknown_modules = set(by_module) - set(DOMAINS)
    require(not unknown_modules, f"add these model modules to DOMAINS: {sorted(unknown_modules)}")
    no_purpose = set(tables) - set(PURPOSE) - {n for n, d in docs.items() if d}
    require(not no_purpose, f"add PURPOSE text or a model docstring for: {sorted(no_purpose)}")
    stale = set(PURPOSE) - set(tables)
    require(not stale, f"PURPOSE names tables that no longer exist: {sorted(stale)}")

    out: list[str] = [BEGIN, ""]
    out.append(
        "_This block is generated by `scripts/docs/gen_db_er.py` from the SQLAlchemy models and the "
        "Alembic migrations. Do not edit it by hand: change the code, then run "
        "`.venv/bin/python scripts/docs/gen_db_er.py`._"
    )
    out.append("")
    out.append(
        f"**Schema at Alembic head `{HEAD}`.** {len(tables)} application tables in "
        f"{len(by_module)} domains, plus {len(sorted(n for n in sql.tables if n.startswith('procrastinate_')))} "
        "job-queue tables owned by procrastinate (not in the models)."
    )
    out.append("")

    # ---- summary -------------------------------------------------------------------------
    out += ["## Domains at a glance", "", "| Domain | Model module | Tables |", "|---|---|---|"]
    for m in DOMAIN_ORDER:
        if m in by_module:
            names = ", ".join(f"[`{t.name}`](#{anchor(t.name)})" for t in by_module[m])
            out.append(f"| {DOMAINS[m][0]} | `app/db/models/{m}.py` | {names} |")
    out.append("")

    # ---- (b) big picture -------------------------------------------------------------------
    out += [
        "## Big picture: how the domains connect",
        "",
        "First the domains alone. An arrow means tables in one domain hold foreign keys "
        "to tables in the other.",
        "",
        domain_map(tables, mod),
        "",
        "How to read it: each box is one domain (one model module). The arrow points from "
        "the domain that holds the foreign key to the domain it references; the label "
        "counts those foreign keys. A dotted arrow into Tenancy means the domain's rows "
        "carry `tenant_id` and actor columns (`tenants`, `tenant_memberships`); nearly every "
        "domain has them, so they are not counted.",
        "",
    ]
    picture, hub_count = big_picture(tables)
    out += [
        f"Then the {hub_count} central tables and the foreign keys between them. Columns are "
        "left out; each domain diagram below shows them.",
        "",
        picture,
        "",
        "How to read it:",
        "- Each box is a table. A line is a foreign key; the label names the child's FK "
        "column(s), and `+N more` means several FKs join the same pair.",
        "- The `||` / `|o` end sits on the parent (the referenced table); `o{` on the child "
        "means one parent row has zero or more child rows.",
        "- Almost every table also has `tenant_id -> tenants`. That line is drawn only for a "
        "table with no other line here. Actor columns (who generated, approved, decided) "
        "point at `tenant_memberships` and are drawn in the domain diagrams instead.",
        "- `category_templates` is platform data with no `tenant_id`; `rules` mixes platform "
        "rows (`tenant_id` NULL) and a brand's own rows. Tenant rows point at both.",
        "",
    ]

    # ---- (a) one diagram per domain --------------------------------------------------------
    out += [
        "## Domain diagrams",
        "",
        "One diagram per model module. A box with columns belongs to the domain; a box "
        "without columns is a table from another domain that this domain points to. Shown "
        "columns: primary key (PK), foreign key columns (FK), single-column unique keys "
        "(UK), `tenant_id`, and status-like columns. Every column is in the "
        "[table catalog](#table-catalog).",
        "",
    ]
    for m in DOMAIN_ORDER:
        if m not in by_module:
            continue
        title, summary = DOMAINS[m]
        diagram, n_entities, n_edges = domain_diagram(m, by_module[m])
        require(n_entities <= 25, f"{m}: {n_entities} entities, split the diagram")
        out += [f"### {title}", "", summary, "", diagram, ""]
        own, other = len(by_module[m]), n_entities - len(by_module[m])
        notes = [
            f"- {own} {'table' if own == 1 else 'tables'} in this domain"
            + (f", {other} from other domains (boxes without columns)" if other else "")
            + f"; {n_edges} relationship {'line' if n_edges == 1 else 'lines'}. "
            '`"nullable"` after a column means it may be NULL.',
        ]
        if m == "platform":
            notes.append(
                "- These tables have no `tenant_id`. Tenant tables point at them (for "
                "example `listing_versions.category_template_id`); those lines are in the "
                "other domains' diagrams."
            )
            out += [*notes, ""]
            continue
        if m == "tenancy":
            notes.append(
                "- Here the `tenant_id -> tenants` lines are drawn. In the other "
                "domains they are left out, since every tenant table has one."
            )
        else:
            notes.append(
                "- The `tenant_id -> tenants` line every tenant table has is left out. "
                "It is drawn only where it says more: a nullable `tenant_id` (platform "
                "rows) or a `tenant_id` primary key (one row per tenant)."
            )
        notes.append(
            "- A label lists the child's FK columns. Composite FKs also carry "
            "`tenant_id`, so a row can only point at a row of the same tenant."
        )
        out += [*notes, ""]

    out += transitions_section(sql.transitions)

    # ---- (c) table catalog ------------------------------------------------------------------
    out += [
        "## Table catalog",
        "",
        "Every table, alphabetically inside its domain. **Tenant-scoped** means the table has "
        "a `tenant_id` column and row-level security (RLS) is enabled and forced, so the app "
        "role sees only the current tenant's rows. CHECK names come from the migrations; an "
        '"allowed values" note is read from a `col IN (...)` CHECK.',
        "",
    ]
    catalogued: list[str] = []
    for m in DOMAIN_ORDER:
        if m not in by_module:
            continue
        out += [f"### {DOMAINS[m][0]} tables", ""]
        for t in by_module[m]:
            catalogued.append(t.name)
            out += table_section(t, sql, col_notes, con_notes, docs)

    # ---- other objects ----------------------------------------------------------------------
    out += ["## Other database objects", ""]
    queue = sorted(n for n in sql.tables if n not in tables and n != "alembic_version")
    out.append(
        "Tables created by migrations that are not ORM models: "
        + ", ".join(f"`{n}`" for n in queue)
        + " (procrastinate's job queue; no tenant data, "
        "no RLS)."
    )
    out.append("")
    out.append(
        "Views: "
        + ", ".join(
            f"`{v}`"
            + (" (security_invoker: RLS applies to the caller)" if v in sql.invoker_views else "")
            for v in sorted(sql.views)
        )
        + ". Both are read-only display projections of listing and product status."
    )
    out.append("")
    out += [
        "Trigger functions and the tables they guard:",
        "",
        "| Function | Tables |",
        "|---|---|",
    ]
    by_fn: dict[str, list[str]] = defaultdict(list)
    for table, trigs in sql.triggers.items():
        if table.startswith("procrastinate_"):
            continue
        for _, (_, fn) in trigs.items():
            if f"`{table}`" not in by_fn[fn]:
                by_fn[fn].append(f"`{table}`")
    for fn in sorted(by_fn):
        out.append(f"| `{fn}()` | {', '.join(sorted(by_fn[fn]))} |")
    out += ["", END]

    missing = [t.name for t in metadata.sorted_tables if t.name not in catalogued]
    require(not missing, f"tables missing from the catalog: {missing}")
    require(
        len(catalogued) == len(set(catalogued)) == len(metadata.sorted_tables),
        "a table is catalogued twice or not at all",
    )
    return "\n".join(out) + "\n"


SIDE_EVENTS = ("edit", "revalidate", "human_task_opened")


def transitions_section(rows: list[tuple[str, str, str, str]]) -> list[str]:
    """The listing status machine, read from the `listing_status_transitions` seed rows."""
    require(rows, "no listing_status_transitions seed rows found in the migrations")
    main: dict[tuple[str, str], set[str]] = defaultdict(set)
    side: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for frm, to, event, _actor in rows:
        if event in SIDE_EVENTS:
            side[event][frm].add(to)
        else:
            main[(frm, to)].add(event)
    lines = [
        "## Listing status machine",
        "",
        f"`listings.status` can only move along the {len(rows)} rows of "
        "`listing_status_transitions`. The `listings_status_guard` trigger refuses any other "
        "move (`INVALID_STATUS_TRANSITION`). This diagram is drawn from those rows.",
        "",
        "```mermaid",
        "stateDiagram-v2",
        "    [*] --> created",
    ]
    for (frm, to), events in sorted(main.items()):
        lines.append(f"    {frm} --> {to} : {', '.join(sorted(events))}")
    lines += [
        "```",
        "",
        "How to read it: a box is a status, an arrow is an allowed move, and the label is the "
        "event (`listings.last_event`) that makes it. Three events are left out of the "
        "picture to keep it readable; they are listed below.",
        "",
        "| Event | From status | To status |",
        "|---|---|---|",
    ]
    for event in SIDE_EVENTS:
        for frm in sorted(side[event]):
            lines.append(
                f"| `{event}` | `{frm}` | "
                + ", ".join(f"`{t}`" for t in sorted(side[event][frm]))
                + " |"
            )
    lines.append("")
    actors = sorted({(e, a) for _, _, e, a in rows})
    by_actor: dict[str, set[str]] = defaultdict(set)
    for e, a in actors:
        by_actor[a].add(e)
    lines.append(
        "Who may fire an event (`actor`): "
        + "; ".join(f"`{a}`: " + ", ".join(sorted(by_actor[a])) for a in sorted(by_actor))
        + ". `user` needs a signed-in person, `worker` a background job, `any` either."
    )
    multi = sorted(e for e in {e for e, _ in actors} if sum(x == e for x, _ in actors) > 1)
    if multi:
        lines.append("")
        lines.append(
            "An event under two actors has a different actor on different rows: "
            + "; ".join(
                f"`{e}` is "
                + " and ".join(
                    f"`{a}` from "
                    + ", ".join(f"`{f}`" for f in sorted({f for f, _, ev, ac in rows
                                                          if ev == e and ac == a}))
                    for a in sorted({ac for _, _, ev, ac in rows if ev == e})
                )
                for e in multi
            )
            + "."
        )  # fmt: skip
    lines.append("")
    return lines


def table_section(t: Table, sql: SqlFacts, col_notes, con_notes, docs) -> list[str]:
    has_tenant = "tenant_id" in t.c
    rls = t.name in sql.rls_enabled
    forced = t.name in sql.rls_forced
    policies = sorted(sql.policies.get(t.name, ()))
    if has_tenant and rls and forced:
        scope = "yes (`tenant_id` + RLS enabled and forced)"
        if t.c.tenant_id.nullable:
            scope += "; `tenant_id` may be NULL for platform rows"
    elif t.name == "tenants" and rls:
        scope = "yes, on its own `id` (RLS enabled and forced)"
    elif rls:
        scope = "no `tenant_id`, but RLS is enabled" + (" and forced" if forced else "")
    else:
        scope = "no (platform-global or tenant-less; no RLS)"
    lines = [f"#### `{t.name}`", ""]
    # The hand-written purpose wins; a table without one falls back to its model docstring.
    purpose = PURPOSE.get(t.name) or docs[t.name]
    lines += [purpose, ""]
    flags = [f"**Tenant-scoped:** {scope}."]
    if policies:
        flags.append("**RLS policies:** " + ", ".join(f"`{p}`" for p in policies) + ".")
    if t.name in sql.unlogged:
        flags.append("**UNLOGGED** table.")
    lines += [" ".join(flags), ""]

    # columns
    fk_by_col: dict[str, list[str]] = defaultdict(list)
    for fk in fks(t):
        naming = set(own_cols(fk))
        for c, p in zip([c.name for c in fk.columns], fk_parent_cols(fk), strict=True):
            if c not in naming:
                continue  # a pinning column of a composite FK (tenant, listing, marketplace)
            od = f", ON DELETE {fk.ondelete}" if fk.ondelete else ""
            note = f"FK -> `{fk.referred_table.name}.{p}`{od}"
            if note not in fk_by_col[c]:
                fk_by_col[c].append(note)
    uniq_single = {u.columns.keys()[0]: u.name for u in uniques(t) if len(u.columns) == 1}
    values: dict[str, list[str]] = {}
    for _, expr in sorted(sql.checks.get(t.name, {}).items()):
        got = allowed_values(expr)
        if got and got[0] in t.c:
            values[got[0]] = got[1]
    lines += ["| Column | Type | Null | Default | Notes |", "|---|---|---|---|---|"]
    for c in t.columns:
        notes = []
        if c.primary_key:
            notes.append("PK")
        notes += fk_by_col.get(c.name, [])
        if c.name in uniq_single:
            notes.append("unique")
        if c.name in values:
            notes.append("allowed: " + ", ".join(f"`{v}`" for v in values[c.name]))
        if (t.name, c.name) in col_notes:
            notes.append(col_notes[(t.name, c.name)])
        lines.append(
            f"| `{c.name}` | {md(pg_type(c))} | {'yes' if c.nullable else 'no'} | "
            f"{md(default_of(c))} | {md('; '.join(notes))} |"
        )
    lines.append("")

    # keys and indexes
    pk = t.primary_key
    keyrows = [f"- Primary key `{pk.name}`: ({', '.join(pk.columns.keys())})"]
    for u in uniques(t):
        note = con_notes.get((t.name, u.name or ""), "")
        keyrows.append(
            f"- Unique `{u.name}`: ({', '.join(u.columns.keys())})" + (f" - {note}" if note else "")
        )
    for i in sorted(t.indexes, key=lambda i: i.name or ""):
        opts = i.dialect_options["postgresql"]
        bits = [f"({index_cols(i)})"]
        if opts.get("using"):
            bits.insert(0, f"USING {opts['using']}")
        if opts.get("include"):
            bits.append("INCLUDE (" + ", ".join(opts["include"]) + ")")
        if opts.get("with"):
            bits.append(
                "WITH (" + ", ".join(f"{k} = {v}" for k, v in sorted(opts["with"].items())) + ")"
            )
        if opts.get("where") is not None:
            bits.append(f"WHERE {sql_text(opts['where'], t.name)}")
        kind = "Unique index" if i.unique else "Index"
        note = con_notes.get((t.name, i.name or ""), "")
        keyrows.append(
            f"- {kind} `{i.name}`: {md(' '.join(bits))}" + (f" - {md(note)}" if note else "")
        )
    lines += ["Keys and indexes:", "", *keyrows, ""]

    comp = [fk for fk in fks(t) if len(fk.columns) > 1]
    if comp:
        lines.append("Composite foreign keys:")
        lines.append("")
        for fk in comp:
            od = f" ON DELETE {fk.ondelete}" if fk.ondelete else ""
            if fk.deferrable:
                od += " DEFERRABLE" + (f" INITIALLY {fk.initially}" if fk.initially else "")
            note = con_notes.get((t.name, fk.name or ""), "")
            lines.append(
                f"- `{fk.name}`: ({', '.join(c.name for c in fk.columns)}) -> "
                f"`{fk.referred_table.name}` ({', '.join(fk_parent_cols(fk))}){od}"
                + (f" - {md(note)}" if note else "")
            )
        lines.append("")
    checks = sorted(sql.checks.get(t.name, {}))
    lines.append(
        "CHECK constraints: " + (", ".join(f"`{c}`" for c in checks) if checks else "none") + "."
    )
    lines.append("")
    trigs = sql.triggers.get(t.name, {})
    if trigs:
        lines.append(
            "Triggers: "
            + ", ".join(f"`{n}` ({trigger_when(when)})" for n, (when, _) in sorted(trigs.items()))
            + "."
        )
        lines.append("")
    return lines


DOMAIN_ORDER = (
    "tenancy", "catalog", "catalog_assets", "facts", "listings", "rules", "runs",
    "issues", "ops", "audit_brand", "platform",
)  # fmt: skip

HEAD = ""


def alembic_head() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory.from_config(Config(str(API_DIR / "alembic.ini"))).get_heads()
    require(len(heads) == 1, f"expected one Alembic head, got {heads}")
    return heads[0]


def write(text: str, check: bool) -> int:
    current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
    if BEGIN in current and END in current:
        before, rest = current.split(BEGIN, 1)
        _, after = rest.split(END, 1)
        new = before + text.rstrip("\n") + after
    else:
        new = (
            "# Database architecture\n\n"
            "_Hand-written introduction goes here; it is kept on regeneration._\n\n" + text
        )
    if not new.endswith("\n"):
        new += "\n"
    if check:
        if new != current:
            sys.stdout.write(
                f"{OUT.relative_to(REPO)} is out of date; run scripts/docs/gen_db_er.py\n"
            )
            return 1
        sys.stdout.write(f"{OUT.relative_to(REPO)} is up to date\n")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(new, encoding="utf-8")
    sys.stdout.write(f"wrote {OUT.relative_to(REPO)}\n")
    return 0


def main() -> int:
    global HEAD
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="fail if the file is out of date")
    args = parser.parse_args()
    HEAD = alembic_head()
    return write(generate(), args.check)


if __name__ == "__main__":
    raise SystemExit(main())
