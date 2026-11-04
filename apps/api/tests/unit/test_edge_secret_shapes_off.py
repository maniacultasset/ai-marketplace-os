"""EDGE-CF fix round 1 F1, re-check (tester, round 2): under ORIGIN_AUTH=off every malformed shape
a hand-made Secret Manager version can take refuses the api boot, names only; a worker never reads
the value; a well-formed rotation pair still boots and vouches."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.deployed_env import DEPLOYED_S15_ENV

A = "Zq9-edge-origin-secret-current-32"
B = "Zq9-edge-origin-secret-previous-3"


def _env(**over: str) -> dict[str, str]:
    return {
        "APP_ENV": "production",
        "APP_ROLE": "api",
        "DATABASE_URL": "postgresql+psycopg://u:p@10.0.0.1:5432/db",
        "JWT_SECRET": "Zq9-not-committed-private-secret-32+",
        "SENTRY_DSN": "https://public@o0.ingest.example.invalid/0",
        **DEPLOYED_S15_ENV,
        "ORIGIN_AUTH": "off",
        **over,
    }


def _boot(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> object:
    from app.core.config import get_settings

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return get_settings()


@pytest.mark.parametrize(
    "bad",
    [
        f"{A}\r\n",  # CRLF from a Windows editor
        f" {A}",  # leading space: an empty first value
        f"{A} ",  # trailing space: an empty second value
        f"{A}  {B}",  # two spaces
        f"{A}\t{B}",  # tab instead of a space
        f"{A}\n{B}",  # newline instead of a space
        f"{A} {B}\n",  # `echo "$NEW $OLD"`
        f"{A[:31]}",  # 31 characters
    ],
)
def test_off_refuses_every_malformed_shape(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    with pytest.raises(SystemExit) as info:
        _boot(monkeypatch, _env(EDGE_ORIGIN_SECRET=bad))
    message = str(info.value.code)
    assert "EDGE_ORIGIN_SECRET" in message
    assert "Zq9" not in message


def test_off_rotation_pair_boots_and_vouches(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = _boot(monkeypatch, _env(EDGE_ORIGIN_SECRET=f"{A} {B}"))
    assert loaded.edge_origin_secrets() == (A.encode(), B.encode())  # type: ignore[attr-defined]


def test_off_unset_or_blank_boots_without_vouching(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for blank in ("", "   "):
        loaded = _boot(monkeypatch, _env(EDGE_ORIGIN_SECRET=blank))
        assert loaded.edge_origin_secrets() == ()  # type: ignore[attr-defined]


def test_worker_role_ignores_a_malformed_secret(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # local: a deployed worker also needs the real AI pipeline installed (an unrelated guard)
    env = _env(APP_ROLE="worker", APP_ENV="local", EDGE_ORIGIN_SECRET="short")
    env["DATABASE_URL"] = "postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os"
    loaded = _boot(monkeypatch, env)
    assert loaded.edge_origin_secrets() == ()  # type: ignore[attr-defined]
