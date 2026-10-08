"""Drive the staff onboarding CLI in-process (the parser plus `run_tenant_command`)."""

from __future__ import annotations

import io
from typing import Any

import httpx
import pytest

PASSWORD = "correct-horse-battery-1"


async def cli(
    argv: list[str], capsys: pytest.CaptureFixture[str], stdin: str = ""
) -> tuple[int, str, str]:
    from app.cli import build_parser, run_tenant_command

    code = await run_tenant_command(build_parser().parse_args(argv), io.StringIO(stdin))
    out = capsys.readouterr()
    return code, out.out, out.err


async def login(api: httpx.AsyncClient, email: str, password: str = PASSWORD) -> httpx.Response:
    return await api.post("/api/v1/auth/login", json={"email": email, "password": password})


def create_argv(slug: str, owner: str, *extra: str) -> list[str]:
    return [
        "tenant",
        "create",
        "--name",
        f"Tenant {slug}",
        "--slug",
        slug,
        "--owner-email",
        owner,
        "--staff-email",
        "staff@ops.test",
        *extra,
    ]


def staff_audit(conn: Any, action: str) -> list[tuple[Any, ...]]:
    return conn.execute(  # type: ignore[no-any-return]
        "SELECT actor_type, actor_email, tenant_id, entity_type, after, request_id FROM audit_log "
        "WHERE action = %s ORDER BY id",
        [action],
    ).fetchall()
