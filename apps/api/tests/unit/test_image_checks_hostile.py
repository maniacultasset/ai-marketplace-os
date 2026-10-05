"""Hostile input never raises; bombs are capped before decode; the function is pure."""

from __future__ import annotations

import warnings
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from app.services.image_checks import MAX_JPEG_SCANS, MAX_PIXELS
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mkos_schemas.images import ImageReport
from PIL import Image, ImageFile, PngImagePlugin
from tests import image_fixtures as fx
from tests.image_fixtures import amazon_rules, codes, names, run

RULES = amazon_rules()


def hostile(data: bytes) -> ImageReport:
    return run(data, RULES)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count Image.open, verify and load calls."""
    counts = {"open": 0, "verify": 0, "load": 0}
    real_open = Image.open
    real_verify = PngImagePlugin.PngImageFile.verify
    real_load = ImageFile.ImageFile.load

    def spy_open(fp: Any, *args: Any, **kwargs: Any) -> Any:
        counts["open"] += 1
        return real_open(fp, *args, **kwargs)

    def spy_verify(self: Any) -> None:
        counts["verify"] += 1
        real_verify(self)

    def spy_load(self: Any) -> Any:
        counts["load"] += 1
        return real_load(self)

    monkeypatch.setattr(Image, "open", spy_open)
    monkeypatch.setattr(PngImagePlugin.PngImageFile, "verify", spy_verify)
    monkeypatch.setattr(ImageFile.ImageFile, "load", spy_load)
    return counts


# 1. decompression bombs and the pixel cap


def test_image_checks_hostile_bomb_header() -> None:
    with pytest.warns(Image.DecompressionBombWarning):
        report = hostile(fx.png_header_only(20000, 5000))
    assert names(report) == ["file_size", "format"]
    assert codes(report) == ["FILE_TOO_LARGE"] and report.pass_ is False


def test_image_checks_hostile_bomb_error() -> None:
    report = hostile(fx.png_header_only(30000, 30000))
    assert codes(report) == ["FILE_TOO_LARGE"]


def test_image_checks_hostile_bomb_warning_as_error() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        report = hostile(fx.png_header_only(20000, 5000))
    assert codes(report) == ["FILE_TOO_LARGE"]


def test_bomb_without_rules_lists_format_and_file_size() -> None:
    report = run(fx.png_header_only(8001, 5000, mode="L"))
    assert names(report) == ["format", "file_size"]


def test_pixel_cap_boundary(calls: dict[str, int]) -> None:
    assert MAX_PIXELS == 8000 * 5000
    at_cap = hostile(fx.png_header_only(8000, 5000, mode="L"))
    assert codes(at_cap) == ["CORRUPT"]
    assert calls["verify"] == 1 and calls["load"] == 1
    calls.update(verify=0, load=0)
    over = hostile(fx.png_header_only(8001, 5000, mode="L"))
    assert codes(over) == ["FILE_TOO_LARGE"]
    assert calls["verify"] == 0 and calls["load"] == 0


# 2, 3. text bomb, multi-frame, zero size


def test_image_checks_hostile_png_text_bomb() -> None:
    data = fx.png(64, 64, text_chunk_bytes=2_000_000)
    assert len(data) < 50_000
    report = hostile(data)
    assert names(report) == ["file_size", "format", "corruption"]
    assert codes(report) == ["CORRUPT"]


def test_image_checks_hostile_multiframe() -> None:
    report = hostile(fx.tiff(1000, 1000, frames=3))
    assert report.pass_ is True
    assert names(report)[-1] == "background"


def test_image_checks_hostile_zero_size() -> None:
    assert codes(hostile(fx.png_header_only(0, 0))) == ["CORRUPT"]
    assert codes(hostile(fx.png_header_only(0, 10))) == ["CORRUPT"]


# 4. property tests

SEEDS: dict[str, bytes] = {
    "png": fx.png(48, 40),
    "jpeg": fx.jpeg(48, 40),
    "jpeg_exif": fx.jpeg(48, 40, exif_orientation=6),
    "tiff": fx.tiff(48, 40),
    "tiff_multiframe": fx.tiff(24, 20, frames=3),
}
MAGIC_PREFIXES = [b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"II*\x00", b"MM\x00*"]


def assert_invariants(report: ImageReport) -> None:
    assert isinstance(report, ImageReport)
    listed = [c.check for c in report.checks]
    assert len(listed) == len(set(listed))
    failed = {c.check for c in report.checks if not c.passed}
    assert {i.check for i in report.issues} == failed
    # every rule in RULES is error severity, so pass == no issue
    assert report.pass_ is (not report.issues)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.binary(max_size=4096))
def test_image_checks_never_raise(data: bytes) -> None:
    assert_invariants(hostile(data))


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.sampled_from(MAGIC_PREFIXES), st.binary(max_size=4096))
def test_image_checks_never_raise_magic_prefix(prefix: bytes, tail: bytes) -> None:
    assert_invariants(hostile(prefix + tail))


def _mutations(seed: bytes) -> st.SearchStrategy[bytes]:
    head = min(512, len(seed) - 1)
    position = st.one_of(
        st.integers(0, head),
        st.integers(0, head),
        st.integers(0, head),
        st.integers(0, len(seed) - 1),
    )
    op = st.one_of(
        st.tuples(st.just("flip"), position, st.integers(1, 255)),
        st.tuples(st.just("insert"), position, st.integers(0, 255)),
        st.tuples(st.just("truncate"), position, st.just(0)),
    )

    def apply(ops: list[tuple[str, int, int]]) -> bytes:
        data = bytearray(seed)
        for kind, pos, value in ops:
            pos = min(pos, max(len(data) - 1, 0))
            if kind == "flip" and data:
                data[pos] ^= value
            elif kind == "insert":
                data.insert(pos, value)
            elif kind == "truncate":
                del data[max(pos, 1) :]
        return bytes(data)

    return st.lists(op, min_size=1, max_size=8).map(apply)


def _mutated_test(name: str) -> Callable[[], None]:
    @settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(_mutations(SEEDS[name]))
    def test(data: bytes) -> None:
        assert_invariants(hostile(data))

    return test


test_image_checks_never_raise_mutated_png = _mutated_test("png")
test_image_checks_never_raise_mutated_jpeg = _mutated_test("jpeg")
test_image_checks_never_raise_mutated_jpeg_exif = _mutated_test("jpeg_exif")
test_image_checks_never_raise_mutated_tiff = _mutated_test("tiff")
test_image_checks_never_raise_mutated_tiff_multiframe = _mutated_test("tiff_multiframe")


# 5-7. purity

FIXTURES: list[bytes] = [
    fx.jpeg(1000, 1000),
    fx.jpeg(1200, 1000, exif_orientation=6),
    fx.png(1000, 1000, mode="RGBA", alpha=0),
    fx.png(1000, 1000, mode="P", transparent_product=True),
    fx.tiff(1000, 1000, mode="CMYK"),
    fx.tiff(500, 500, frames=2),
    fx.magic("gif"),
    fx.magic("bigtiff"),
    b"",
    fx.truncated(fx.jpeg(256, 256), 400),
    fx.png(64, 64, text_chunk_bytes=2_000_000),
    fx.png_header_only(8001, 5000, mode="L"),
    fx.jpeg_scans(MAX_JPEG_SCANS + 1),
    fx.jpeg_with_exif_thumbnail(),
]


def _globals() -> tuple[object, ...]:
    return (
        Image.MAX_IMAGE_PIXELS,
        ImageFile.LOAD_TRUNCATED_IMAGES,
        PngImagePlugin.MAX_TEXT_CHUNK,
        list(warnings.filters),
    )


def test_pillow_globals_untouched() -> None:
    before = _globals()
    for data in FIXTURES:
        hostile(data)
        assert _globals() == before


def test_image_checks_deterministic() -> None:
    for data in FIXTURES:
        assert hostile(data).model_dump_json() == hostile(data).model_dump_json()


def test_image_checks_thread_safe() -> None:
    inputs = (FIXTURES * 3)[:32]
    sequential = [hostile(d).model_dump_json() for d in inputs]
    with ThreadPoolExecutor(8) as pool:
        threaded = list(pool.map(lambda d: hostile(d).model_dump_json(), inputs))
    assert threaded == sequential


# 8. JPEG scan bomb


def test_image_checks_hostile_scan_bomb(calls: dict[str, int]) -> None:
    report = run(fx.jpeg_scans(MAX_JPEG_SCANS + 1))
    assert names(report) == ["format", "corruption"]
    assert codes(report) == ["CORRUPT"]
    assert calls["open"] == 0
    at_cap = run(fx.jpeg_scans(MAX_JPEG_SCANS))
    assert codes(at_cap) == ["CORRUPT"]
    assert calls["open"] >= 1
