"""Shared fixtures. Nothing here imports `app` at module scope (a bad env must never
abort collection), so `app.*` is always imported lazily inside a fixture."""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest
import yaml

if TYPE_CHECKING:
    from fastapi import FastAPI

REPO_ROOT = Path(__file__).resolve().parents[3]

CI_JWT_SECRET = "ci_only_not_a_real_secret_at_least_32_chars"  # A§11:264
CI_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/mos_test"
_EXTRA_ENV_NAMES = ("ENV", "REDIS_URL")
_REGISTER_ROW = re.compile(r"^\| `([A-Z][A-Z0-9_]*)` \|")


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


def settings_env_names() -> list[str]:
    from app.core.config import Settings

    return [name.upper() for name in Settings.model_fields]


@pytest.fixture
def clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A process env holding no Settings variable, a cwd with no `.env`, and a
    cold settings cache, before and after the test."""
    from app.core.config import get_settings

    monkeypatch.chdir(tmp_path)
    for name in (*settings_env_names(), *_EXTRA_ENV_NAMES):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def ci_env(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The three-variable CI env block (CANON.md:21; A§11:264)."""
    env = {"APP_ENV": "local", "DATABASE_URL": CI_DATABASE_URL, "JWT_SECRET": CI_JWT_SECRET}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture
def make_app(ci_env: dict[str, str]) -> Callable[[], FastAPI]:
    def factory() -> FastAPI:
        from app.main import create_app

        return create_app()

    return factory


def _normalise_yaml_keys(node: Any) -> Any:
    # YAML 1.1 reads a bare `on:` key as the boolean True.
    if isinstance(node, dict):
        return {("on" if k is True else k): _normalise_yaml_keys(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_normalise_yaml_keys(item) for item in node]
    return node


def read_yaml(path: Path) -> Any:
    return _normalise_yaml_keys(yaml.safe_load(path.read_text(encoding="utf-8")))


@pytest.fixture
def load_yaml() -> Callable[[Path], Any]:
    return read_yaml


def parse_register(path: Path) -> list[str]:
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _REGISTER_ROW.match(line)
        if match:
            names.append(match.group(1))
    return names


@pytest.fixture
def register_names() -> list[str]:
    return parse_register(REPO_ROOT / "docs" / "canon" / "ENV.md")


class FakeConnection:
    def __init__(self, engine: FakeEngine) -> None:
        self._engine = engine

    async def __aenter__(self) -> FakeConnection:
        self._engine.connects += 1
        self._engine.entered.set()
        if self._engine.delay:
            await asyncio.sleep(self._engine.delay)
        if self._engine.error is not None:
            raise self._engine.error
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, statement: object) -> None:
        self._engine.executed.append(str(statement))


class FakeEngine:
    """Stands in for the AsyncEngine: `connect()` can fail, hang, or run statements."""

    def __init__(self, *, error: Exception | None = None, delay: float = 0.0) -> None:
        self.error = error
        self.delay = delay
        self.connects = 0
        self.entered = asyncio.Event()
        self.executed: list[str] = []

    def connect(self) -> FakeConnection:
        return FakeConnection(self)


@pytest.fixture
def fake_engine(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeEngine]:
    """Install a FakeEngine as `app.db.session.get_engine()` for this test."""

    def install(*, error: Exception | None = None, delay: float = 0.0) -> FakeEngine:
        engine = FakeEngine(error=error, delay=delay)
        monkeypatch.setattr("app.db.session.get_engine", lambda: engine)
        return engine

    return install


def load_script(relative: str) -> ModuleType:
    """Import a stdlib-only script under scripts/ (not a package) as a module."""
    path = REPO_ROOT / relative
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def script() -> Callable[[str], ModuleType]:
    return load_script
