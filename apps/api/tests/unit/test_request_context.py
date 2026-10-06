"""S11 T3: the per-request context (request id and tenant) held in one contextvar."""

from __future__ import annotations

import asyncio
import contextvars
import re
import threading
import uuid

import pytest
from app.core.request_context import (
    REQUEST_ID_HEADER,
    accept_request_id,
    bind_tenant,
    current_request_id,
    current_tenant_id,
    new_request_id,
    request_context,
)
from hypothesis import given
from hypothesis import strategies as st

VALID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def _is_uuid4(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value


def test_header_name_is_lowercase() -> None:
    assert REQUEST_ID_HEADER == "x-request-id"


@pytest.mark.parametrize("raw", ["abc", "a", "x" * 128, "a.b:c-d_e", "0f8fad5b-d9cb-469f"])
def test_accept_valid_ids(raw: str) -> None:
    assert accept_request_id(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [None, "", " ", "x" * 129, "a\r\nb", "a b", "é", 'a"b', "<script>", "a\x00b", "abc\n", " abc"],
)
def test_replace_invalid_ids(raw: str | None) -> None:
    result = accept_request_id(raw)
    assert result != raw
    assert _is_uuid4(result)


def test_new_request_id_is_uuid4() -> None:
    first, second = new_request_id(), new_request_id()
    assert _is_uuid4(first)
    assert first != second


def test_context_resets_after_block() -> None:
    assert current_request_id() is None
    with request_context("abc") as ctx:
        assert ctx.request_id == "abc"
        assert current_request_id() == "abc"
    assert current_request_id() is None


def test_context_resets_after_exception() -> None:
    with pytest.raises(RuntimeError), request_context("abc"):
        bind_tenant("t1")
        raise RuntimeError("boom")
    assert current_request_id() is None
    assert current_tenant_id() is None


def test_nested_context_restores_outer() -> None:
    with request_context("outer"):
        bind_tenant("t-outer")
        with request_context("inner"):
            assert current_request_id() == "inner"
            assert current_tenant_id() is None
        assert current_request_id() == "outer"
        assert current_tenant_id() == "t-outer"
    assert current_request_id() is None


def test_bind_tenant_outside_request_is_noop() -> None:
    bind_tenant(uuid.uuid4())
    assert current_tenant_id() is None
    assert current_request_id() is None


def test_bind_tenant_accepts_uuid() -> None:
    tid = uuid.uuid4()
    with request_context("abc"):
        bind_tenant(tid)
        assert current_tenant_id() == str(tid)


def test_bind_tenant_visible_after_thread_copy() -> None:
    # A sync FastAPI dependency runs in a worker thread on a copy of the context: the mutable
    # holder makes its bind visible to the request's own context.
    with request_context("abc"):
        copied = contextvars.copy_context()
        worker = threading.Thread(target=copied.run, args=(bind_tenant, "t-thread"))
        worker.start()
        worker.join()
        assert current_tenant_id() == "t-thread"


async def test_contexts_isolated_across_tasks() -> None:
    async def one(i: int) -> tuple[str | None, str | None]:
        with request_context(f"req-{i}"):
            bind_tenant(f"t-{i}")
            await asyncio.sleep(0.001 * (i % 5))
            return current_request_id(), current_tenant_id()

    results = await asyncio.gather(*(one(i) for i in range(50)))
    assert results == [(f"req-{i}", f"t-{i}") for i in range(50)]
    assert current_request_id() is None


@given(st.one_of(st.none(), st.text(max_size=300)))
def test_accept_request_id_output_always_valid(raw: str | None) -> None:
    result = accept_request_id(raw)
    assert VALID.fullmatch(result)
    if raw is not None and VALID.fullmatch(raw):
        assert result == raw
