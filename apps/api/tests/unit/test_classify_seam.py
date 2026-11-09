"""SI14 T10: the classify and column-map seams look their entry up lazily."""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest
from app.workers import classify_seam, column_map_seam

SEAMS = [
    (classify_seam, "call_classify_product", "classify_product"),
    (column_map_seam, "call_map_columns", "map_columns"),
]


def _fake_pipeline(monkeypatch: pytest.MonkeyPatch, **attrs: Any) -> None:
    module = types.ModuleType("mkos_pipeline")
    for name, value in attrs.items():
        setattr(module, name, value)
    monkeypatch.setattr(
        "importlib.import_module", lambda name: module if name == "mkos_pipeline" else None
    )


@pytest.mark.parametrize(("seam", "call", "entry"), SEAMS)
def test_absent_entry_is_none(
    monkeypatch: pytest.MonkeyPatch, seam: Any, call: str, entry: str
) -> None:
    _fake_pipeline(monkeypatch)
    assert asyncio.run(getattr(seam, call)(object(), object())) is None


@pytest.mark.parametrize(("seam", "call", "entry"), SEAMS)
def test_non_callable_entry_is_none(
    monkeypatch: pytest.MonkeyPatch, seam: Any, call: str, entry: str
) -> None:
    _fake_pipeline(monkeypatch, **{entry: "not callable"})
    assert asyncio.run(getattr(seam, call)(object(), object())) is None


@pytest.mark.parametrize(("seam", "call", "entry"), SEAMS)
def test_entry_exception_propagates(
    monkeypatch: pytest.MonkeyPatch, seam: Any, call: str, entry: str
) -> None:
    async def boom(request: Any, credentials: Any) -> Any:
        raise RuntimeError("pipeline failed")

    _fake_pipeline(monkeypatch, **{entry: boom})
    with pytest.raises(RuntimeError, match="pipeline failed"):
        asyncio.run(getattr(seam, call)(object(), object()))


@pytest.mark.parametrize(("seam", "call", "entry"), SEAMS)
def test_entry_answer_is_returned(
    monkeypatch: pytest.MonkeyPatch, seam: Any, call: str, entry: str
) -> None:
    async def answer(request: Any, credentials: Any) -> Any:
        return ("answered", request, credentials)

    _fake_pipeline(monkeypatch, **{entry: answer})
    assert asyncio.run(getattr(seam, call)("req", "creds")) == ("answered", "req", "creds")


def test_the_stub_pipeline_has_both_entries() -> None:
    import mkos_pipeline

    assert callable(mkos_pipeline.classify_product) and callable(mkos_pipeline.map_columns)
