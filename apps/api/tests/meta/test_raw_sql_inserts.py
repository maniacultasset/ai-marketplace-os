"""S11 T11 (AC 6): rows in the seven `request_id` tables are inserted through the ORM or a Core
`Table`, whose column default stamps the request id. A raw SQL `INSERT` would skip it and leave
the column NULL with no error. The only SQL-side writer allowed is the `audit()` function."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

RAW_INSERT = re.compile(
    r"INSERT\s+INTO\s+(\"?public\"?\s*\.\s*)?\"?"
    r"(audit_log|review_decisions|validation_runs|batches|job_runs|agent_runs|human_tasks)\b",
    re.IGNORECASE,
)
# purge_tenant (0004/18) records its own run: a system actor with no request (it runs from the
# migrator, never inside an HTTP request, so request_id is rightly NULL), and its `after` carries
# mode and row counts, which audit()'s tenant allow-list would strip.
# 0011 (DPP-1) redefines purge_tenant with the same tombstone; its down file restores 0004's body.
ALLOWED = {
    "apps/api/alembic/sql/0004/18_purge_tenant.up.sql: INSERT INTO public.audit_log",
    "apps/api/alembic/sql/0011/01_erasure.up.sql: INSERT INTO public.audit_log",
    "apps/api/alembic/sql/0011/01_erasure.down.sql: INSERT INTO public.audit_log",
}
DEFINES_AUDIT = re.compile(r"CREATE\s+(OR\s+REPLACE\s+)?FUNCTION\s+(public\.)?audit\s*\(", re.I)


def _string_literals(source: str) -> Iterator[str]:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def raw_insert_hits(text: str) -> list[str]:
    return [m.group(0) for m in RAW_INSERT.finditer(text)]


def _offenders(repo_root: Path) -> list[str]:
    found: list[str] = []
    api = repo_root / "apps/api"
    for path in sorted((api / "app").rglob("*.py")):
        for literal in _string_literals(path.read_text(encoding="utf-8")):
            found += [f"{path.relative_to(repo_root)}: {hit}" for hit in raw_insert_hits(literal)]
    migration_files = [
        *sorted((api / "alembic/sql").rglob("*.sql")),
        *sorted((api / "alembic/versions").rglob("*.py")),
    ]
    for path in migration_files:
        text = path.read_text(encoding="utf-8")
        if DEFINES_AUDIT.search(text):
            continue  # the one SQL-side writer: audit() inserts into audit_log
        found += [f"{path.relative_to(repo_root)}: {hit}" for hit in raw_insert_hits(text)]
    return [hit for hit in found if hit not in ALLOWED]


def test_no_raw_sql_insert_into_request_id_tables(repo_root: Path) -> None:
    assert _offenders(repo_root) == []


def test_raw_sql_scan_catches_seeded_insert() -> None:
    sample = 'text("""insert  into\n job_runs (id) VALUES (1)""")'
    hits = [h for literal in _string_literals(sample) for h in raw_insert_hits(literal)]
    assert hits
    assert raw_insert_hits('INSERT INTO "public"."agent_runs" (id)')
    assert raw_insert_hits("insert into public.audit_log values ()")
    assert not raw_insert_hits("INSERT INTO job_runs_archive (id)")
    assert not raw_insert_hits("SELECT * FROM job_runs")
