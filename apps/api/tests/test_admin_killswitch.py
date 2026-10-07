"""CANON-ADDENDUM.md A§14 (S14b T16; Done-when 4): in production `/internal/admin` is a plain 404,
sqladmin is never imported, and `ADMIN_ENABLED=true` refuses to boot.

`PROD_ENV` is A§14's production fixture reduced to the CI block plus SENTRY_DSN (CANON.md:21:
psycopg, no `REDIS_URL`; SENTRY_DSN is required outside local since S11)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from tests.support.deployed_env import DEPLOYED_S15_ENV

PROD_ENV = {
    "APP_ENV": "production",
    "DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:5432/x",
    "JWT_SECRET": "x" * 32,
    # A§14's shape with a `.invalid` host, so no run can ever reach a real Sentry project
    "SENTRY_DSN": "https://public@o0.ingest.example.invalid/0",
    "ADMIN_ENABLED": "false",
    **DEPLOYED_S15_ENV,
}
NON_COMMITTED_SECRET = "Zq9-admin-session-not-committed-32+"


@pytest.fixture
def prod_env(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    from app.core.config import get_settings

    for key, value in PROD_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield dict(PROD_ENV)
    get_settings.cache_clear()


async def test_admin_killswitch_404(
    prod_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main
    from app.main import create_app

    monkeypatch.setattr(app.main, "init_sentry", lambda _: False)  # no client, no flush on exit

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for path in ("/internal/admin", "/internal/admin/", "/internal/admin/listing/list"):
            response = await client.get(path)
            assert response.status_code == 404, path
            assert response.json()["error"]["type"] == "NOT_FOUND"


def test_admin_killswitch_never_imports_sqladmin(clean_env: Path) -> None:
    code = (
        "import json, sys\n"
        "from app.main import create_app\n"
        "create_app()\n"
        "print(json.dumps({'sqladmin': 'sqladmin' in sys.modules, "
        "'admin': 'app.admin' in sys.modules}))\n"
    )
    keep = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "VIRTUAL_ENV"}}
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=clean_env,
        env={**keep, **PROD_ENV, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"sqladmin": False, "admin": False}


def test_admin_killswitch_enabled_exits(
    prod_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("ADMIN_ENABLED", "true")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", NON_COMMITTED_SECRET)
    get_settings.cache_clear()
    with pytest.raises(SystemExit) as info:
        get_settings()
    assert isinstance(info.value.code, str)
    assert "APP_ENV=production refuses ADMIN_ENABLED=true" in info.value.code
    assert NON_COMMITTED_SECRET not in info.value.code
