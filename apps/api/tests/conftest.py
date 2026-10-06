"""Shared fixtures. Nothing here imports `app` at module scope (a bad env must never
abort collection), so `app.*` is always imported lazily inside a fixture."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import io
import json
import logging
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

CI_JWT_SECRET = "ci_only_not_a_real_secret_at_least_32_chars"  # A§11:290
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
    """The three-variable CI env block (CANON.md:21; A§11:290)."""
    env = {"APP_ENV": "local", "DATABASE_URL": CI_DATABASE_URL, "JWT_SECRET": CI_JWT_SECRET}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture(scope="module")
def v1_app(tmp_path_factory: pytest.TempPathFactory) -> Iterator[FastAPI]:
    """One `create_app()` under the CI env block, shared by a module's parametrized surface tests
    (the per-test `make_app` would rebuild the app and its OpenAPI for every case). Its auth is
    the presence-only fake (`tests/support/fake_auth.py`): these suites have no database."""
    from app.core.config import get_settings
    from app.main import create_app
    from tests.support.fake_auth import install_fake_auth

    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(tmp_path_factory.mktemp("v1_app"))
        for name in (*settings_env_names(), *_EXTRA_ENV_NAMES):
            mp.delenv(name, raising=False)
        mp.setenv("APP_ENV", "local")
        mp.setenv("DATABASE_URL", CI_DATABASE_URL)
        mp.setenv("JWT_SECRET", CI_JWT_SECRET)
        get_settings.cache_clear()
        try:
            yield install_fake_auth(create_app())
        finally:
            get_settings.cache_clear()


@pytest.fixture
def make_app(ci_env: dict[str, str]) -> Callable[[], FastAPI]:
    """A fresh `create_app()` with the presence-only fake auth (no database)."""

    def factory() -> FastAPI:
        from app.main import create_app
        from tests.support.fake_auth import install_fake_auth

        return install_fake_auth(create_app())  # type: ignore[no-any-return]

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


class LogCapture:
    """The JSON lines written by `configure_logging(settings, stream=...)`."""

    def __init__(self) -> None:
        self.stream = io.StringIO()

    def text(self) -> str:
        return self.stream.getvalue()

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.text().splitlines() if line.strip()]

    def clear(self) -> None:
        self.stream.seek(0)
        self.stream.truncate()


def reset_logging(root_level: int = logging.WARNING) -> None:
    """Undo `configure_logging()`: drop its root handler, restore the root level, and reset
    structlog and the trace-fields hook."""
    import structlog

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_mkos_root_handler", False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(root_level)
    structlog.reset_defaults()
    if "app.core.logging" in sys.modules:
        sys.modules["app.core.logging"].set_trace_fields_hook(None)


@pytest.fixture(autouse=True)
def _isolate_logging() -> Iterator[None]:
    """`create_app()` configures process-wide logging; no test may inherit another's."""
    level = logging.getLogger().level
    yield
    reset_logging(level)


@pytest.fixture
def log_lines() -> Iterator[Callable[..., LogCapture]]:
    """`start(**settings)` (re)configures logging into a StringIO and returns the capture. Call it
    after `create_app()`, which configures logging to stdout."""

    def start(**overrides: Any) -> LogCapture:
        from app.core.config import Settings
        from app.core.logging import configure_logging

        values: dict[str, Any] = {"app_env": "local", "app_role": "shell", "log_format": "json"}
        values.update(overrides)
        capture = LogCapture()
        configure_logging(Settings(_env_file=None, **values), stream=capture.stream)
        return capture

    yield start


class SentryCapture:
    """Every envelope the Sentry client would have sent, kept in memory (no network)."""

    def __init__(self) -> None:
        self.envelopes: list[Any] = []

    def items(self, kind: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for envelope in self.envelopes:
            for item in envelope.items:
                if item.type == kind and item.payload.json is not None:
                    out.append(item.payload.json)
        return out

    def events(self) -> list[dict[str, Any]]:
        return self.items("event")

    def transactions(self) -> list[dict[str, Any]]:
        return self.items("transaction")

    def serialized(self) -> str:
        """Every envelope as sent, minus the frames' source lines: those quote this repo's test
        code (which holds the planted values), not data from the request."""
        parts: list[str] = []
        for envelope in self.envelopes:
            parts.append(json.dumps(envelope.headers, default=str))
            for item in envelope.items:
                payload = item.payload.json
                if payload is None:
                    parts.append(item.payload.get_bytes().decode("utf-8", "replace"))
                    continue
                cleaned = copy.deepcopy(payload)
                _drop_source_context(cleaned)
                parts.append(json.dumps(cleaned, default=str))
        return "\n".join(parts)


def _drop_source_context(node: Any) -> None:
    if isinstance(node, dict):
        for key in ("pre_context", "context_line", "post_context"):
            node.pop(key, None)
        for value in node.values():
            _drop_source_context(value)
    elif isinstance(node, list):
        for value in node:
            _drop_source_context(value)


@pytest.fixture
def sentry_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[SentryCapture]:
    """Route `init_sentry()` (and so `create_app()` with a DSN) to an in-memory transport; close
    and unbind the client afterwards."""
    import sentry_sdk
    from sentry_sdk.transport import Transport

    capture = SentryCapture()

    class CapturingTransport(Transport):
        def capture_envelope(self, envelope: Any) -> None:
            capture.envelopes.append(envelope)

    monkeypatch.setattr("app.core.observability.DEFAULT_TRANSPORT", CapturingTransport())
    yield capture
    client = sentry_sdk.get_client()
    client.flush(timeout=2)
    client.close()
    sentry_sdk.get_global_scope().set_client(None)
