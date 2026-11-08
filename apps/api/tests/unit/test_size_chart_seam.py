"""SI12 T15 (AC7): the lazy `read_size_chart` lookup on the installed pipeline."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from app.workers import size_chart_seam


def _fake_pipeline(monkeypatch: pytest.MonkeyPatch, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType("mkos_pipeline")
    for name, value in attrs.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "mkos_pipeline", module)
    return module


async def test_absent_entry_point_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_pipeline(monkeypatch)
    assert await size_chart_seam.call_read_size_chart(object(), object()) is None  # type: ignore[arg-type]


async def test_non_callable_entry_point_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_pipeline(monkeypatch, read_size_chart="not a function")
    assert await size_chart_seam.call_read_size_chart(object(), object()) is None  # type: ignore[arg-type]


async def test_present_entry_point_awaited_once_with_the_same_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, Any]] = []
    answer = object()

    async def read_size_chart(request: Any, credentials: Any) -> Any:
        calls.append((request, credentials))
        return answer

    _fake_pipeline(monkeypatch, read_size_chart=read_size_chart)
    request, credentials = object(), object()
    got = await size_chart_seam.call_read_size_chart(request, credentials)  # type: ignore[arg-type]
    assert got is answer
    assert len(calls) == 1 and calls[0][0] is request and calls[0][1] is credentials


async def test_exception_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def read_size_chart(request: Any, credentials: Any) -> Any:
        raise RuntimeError("model down")

    _fake_pipeline(monkeypatch, read_size_chart=read_size_chart)
    with pytest.raises(RuntimeError, match="model down"):
        await size_chart_seam.call_read_size_chart(object(), object())  # type: ignore[arg-type]


def test_importing_the_seam_does_not_import_the_pipeline() -> None:
    source = size_chart_seam.__loader__.get_source(size_chart_seam.__name__)  # type: ignore[union-attr]
    assert source is not None and "import mkos_pipeline" not in source


async def test_the_in_repo_stub_has_the_entry_point() -> None:
    import mkos_pipeline

    assert callable(getattr(mkos_pipeline, "read_size_chart", None))
