"""SI1 T15 (CP17): the derive child runs on its own, behind a bounded reader and a timeout,
and never raises or returns a report."""

from __future__ import annotations

import asyncio
import io
import uuid
from typing import Any

import pytest
from app.services import image_runner
from app.services.image_runner import DerivedImages, parse_derived, run_derive_isolated
from PIL import Image
from tests import image_fixtures as fx

FAKES = "tests.support.image_derive_fakes"
IMAGE = uuid.UUID("00000000-0000-4000-8000-0000000000d1")


async def test_derive_child_on_gps_fixture() -> None:
    found = await run_derive_isolated(fx.jpeg_with_gps_orientation6(40, 20), image_id=IMAGE)
    assert isinstance(found, DerivedImages)
    assert found.clean_mime == "image/jpeg"
    img = Image.open(io.BytesIO(found.clean))
    assert img.size == (20, 40) and len(img.getexif()) == 0
    assert (found.vision_w, found.vision_h) == (20, 40)


async def test_derive_child_corrupt_image_is_none() -> None:
    assert await run_derive_isolated(fx.truncated(fx.jpeg(64, 64), 50), image_id=IMAGE) is None


@pytest.fixture
def fake_child(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr(image_runner, "DERIVE_MODULE", FAKES)
    return monkeypatch


async def test_over_cap_stdout_is_none(fake_child: pytest.MonkeyPatch) -> None:
    fake_child.setattr(image_runner, "DERIVE_STDOUT_CAP", 100_000)
    reads: list[int] = []
    real = image_runner._read_capped

    async def spy(stream: Any, cap: int) -> Any:
        out = await real(stream, cap)
        reads.append(-1 if out is None else len(out))
        return out

    fake_child.setattr(image_runner, "_read_capped", spy)
    assert await run_derive_isolated(b"B64", image_id=IMAGE) is None
    assert reads == [-1]


async def test_read_capped_stops_at_the_cap() -> None:
    stream = asyncio.StreamReader()
    stream.feed_data(b"x" * 300_000)
    stream.feed_eof()
    assert await image_runner._read_capped(stream, 100_000) is None
    small = asyncio.StreamReader()
    small.feed_data(b"abc")
    small.feed_eof()
    assert await image_runner._read_capped(small, 100_000) == b"abc"


async def test_timeout_kills_and_is_none(fake_child: pytest.MonkeyPatch) -> None:
    fake_child.setattr(image_runner, "DERIVE_TIMEOUT_S", 1)
    procs: list[Any] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args: Any, **kwargs: Any) -> Any:
        proc = await real(*args, **kwargs)
        procs.append(proc)
        return proc

    fake_child.setattr(asyncio, "create_subprocess_exec", spy)
    assert await run_derive_isolated(b"S", image_id=IMAGE) is None
    assert procs and procs[0].returncode is not None


@pytest.mark.parametrize("mode", [b"X", b"G", b"N"])
async def test_exit_137_garbage_and_null_are_none(
    fake_child: pytest.MonkeyPatch, mode: bytes
) -> None:
    assert await run_derive_isolated(mode, image_id=IMAGE) is None


async def test_spawn_failure_is_retryable(fake_child: pytest.MonkeyPatch) -> None:
    from app.workers.errors import RetryableJobError

    async def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("no fork")

    fake_child.setattr(asyncio, "create_subprocess_exec", boom)
    with pytest.raises(RetryableJobError):
        await run_derive_isolated(b"N", image_id=IMAGE)


@pytest.mark.parametrize(
    "stdout",
    [
        b"null",
        b"[]",
        b'{"clean_b64": "!!", "clean_mime": "image/jpeg", "vision_b64": "", "vision_w": 1, "vision_h": 1}',
        b'{"clean_b64": "/9j/", "clean_mime": "image/webp", "vision_b64": "/9j/", "vision_w": 1, "vision_h": 1}',
        b'{"clean_b64": "/9j/", "clean_mime": "image/jpeg", "vision_b64": "/9j/", "vision_w": 0, "vision_h": 1}',
        b'{"clean_b64": "/9j/", "clean_mime": "image/jpeg", "vision_b64": "/9j/", "vision_w": 2000, "vision_h": 1}',
        b'{"clean_b64": "iVBORw0KGgo=", "clean_mime": "image/jpeg", "vision_b64": "/9j/", "vision_w": 1, "vision_h": 1}',
    ],
)
def test_parse_refuses_bad_shapes(stdout: bytes) -> None:
    assert parse_derived(stdout) is None


def test_parse_accepts_a_good_shape() -> None:
    good = b'{"clean_b64": "/9j/", "clean_mime": "image/jpeg", "vision_b64": "/9j/", "vision_w": 4, "vision_h": 3}'
    found = parse_derived(good)
    assert found is not None and (found.vision_w, found.vision_h) == (4, 3)
