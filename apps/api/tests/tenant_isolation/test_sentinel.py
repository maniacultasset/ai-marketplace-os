"""The tenant-isolation job's wiring sentinel (ADR-0013 §4). It proves only that the marker, the
job and the dedicated `mos_iso` database are wired; S9 replaces it with the mapper-looping suite
(CANON.md:137).

S9 follow-up (KNOWN-ISSUES.md:30): this job connects as the superuser `postgres`, which bypasses
row-level security even under FORCE ROW LEVEL SECURITY (CANON.md:98). S9 runs the suite as `app_rw`
and asserts `NOT rolsuper AND NOT rolbypassrls` for `current_user`. S1 deliberately asserts only the
database name, never the user.
"""

from __future__ import annotations

import os

import psycopg
import pytest


@pytest.mark.tenant_isolation
@pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])  # psycopg resolves in Python
def test_isolation_job_uses_mos_iso() -> None:
    url = os.environ.get("DATABASE_URL")
    assert url, "DATABASE_URL is unset: run through `make tenant-isolation`"
    libpq = url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(libpq, connect_timeout=5) as conn:
        row = conn.execute("SELECT current_database()").fetchone()
    assert row is not None
    assert row[0] == "mos_iso"
