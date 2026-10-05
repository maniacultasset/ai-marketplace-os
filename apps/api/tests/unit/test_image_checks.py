"""Intrinsic image checks (format, corruption, EXIF orientation) and report assembly."""

from __future__ import annotations

import json
import struct
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from app.services import image_checks
from app.services.image_checks import CHECK_CODES, DEFAULT_SUGGESTIONS, _jpeg_scan_count
from mkos_schemas.codes import ImageIssueCode
from mkos_schemas.images import ImageReport
from PIL import Image, ImageFile
from pydantic import ValidationError
from tests import image_fixtures as fx
from tests.image_fixtures import codes, names, run

INTRINSIC = ["format", "corruption", "exif_orientation"]


@pytest.fixture
def open_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Spy on PIL.Image.open: records each call's keyword arguments."""
    calls: list[dict[str, Any]] = []
    real = Image.open

    def spy(fp: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return real(fp, *args, **kwargs)

    monkeypatch.setattr(Image, "open", spy)
    return calls


# 1. format


@pytest.mark.parametrize(
    "data", [fx.jpeg(64, 64), fx.png(64, 64), fx.tiff(64, 64)], ids=["jpeg", "png", "tiff"]
)
def test_pillow_checks_format_pass(data: bytes) -> None:
    report = run(data)
    assert names(report) == INTRINSIC
    assert report.pass_ is True


@pytest.mark.parametrize(
    "data",
    [
        fx.magic("gif"),
        fx.magic("bmp"),
        fx.magic("webp"),
        fx.magic("svg"),
        fx.magic("bigtiff"),
        b"",
        bytes((i * 37 + 11) % 256 for i in range(256)),
    ],
    ids=["gif", "bmp", "webp", "svg", "bigtiff", "empty", "random"],
)
def test_pillow_checks_format_fail(data: bytes, open_calls: list[dict[str, Any]]) -> None:
    report = run(data)
    assert names(report) == ["format"]
    assert codes(report) == ["FORMAT_UNSUPPORTED"]
    assert report.pass_ is False
    assert open_calls == []


# 2. corruption


def _bad_png_crc() -> bytes:
    data = bytearray(fx.png(64, 64))
    idat = data.index(b"IDAT")
    data[idat + 6] ^= 0xFF
    return bytes(data)


def _truncated_jpeg() -> bytes:
    data = fx.jpeg(256, 256)
    sos = data.index(b"\xff\xda")
    return fx.truncated(data, sos + (len(data) - sos) // 2)


def test_pillow_checks_corruption_pass() -> None:
    report = run(fx.jpeg(128, 96))
    assert report.checks[1].check == "corruption" and report.checks[1].passed


@pytest.mark.parametrize(
    "build",
    [
        _truncated_jpeg,
        _bad_png_crc,
        lambda: fx.truncated(fx.tiff(64, 64), 200),
    ],
    ids=["truncated_jpeg", "bad_png_crc", "truncated_tiff"],
)
def test_pillow_checks_corruption_fail(build: Callable[[], bytes]) -> None:
    report = run(build())
    assert names(report) == ["format", "corruption"]
    assert codes(report) == ["CORRUPT"]
    assert report.pass_ is False


def test_truncated_jpeg_passes_scan_walk() -> None:
    assert _jpeg_scan_count(_truncated_jpeg()) == 1


# 3, 4. report


def test_image_report_checks_listed() -> None:
    report = run(fx.jpeg(200, 200))
    assert names(report) == INTRINSIC
    assert all(c.passed and c.method == "deterministic" for c in report.checks)
    assert report.pass_ is True and report.issues == []
    assert (report.confidence, report.model_used, report.agent_run_id) == (None, None, None)
    assert report.processed_at == fx.NOW
    assert report.image_id == fx.IMAGE_ID


def test_image_report_serialises_pass(repo_root: Path) -> None:
    report = run(fx.magic("gif"))
    dumped = report.model_dump(mode="json")
    assert "pass" in dumped and "pass_" not in dumped
    assert dumped["pass"] is False
    assert ImageReport.model_validate_json(report.model_dump_json()) == report
    schema = json.loads(
        (repo_root / "packages/schemas/jsonschema/ImageReport.schema.json").read_text("utf-8")
    )
    assert set(dumped) == set(schema["properties"])
    assert "pass" in schema["required"]


# 5. frozen maps


def test_check_code_map_frozen() -> None:
    assert dict(CHECK_CODES) == {
        "file_size": ImageIssueCode.FILE_TOO_LARGE,
        "format": ImageIssueCode.FORMAT_UNSUPPORTED,
        "corruption": ImageIssueCode.CORRUPT,
        "resolution": ImageIssueCode.RESOLUTION_LOW,
        "aspect_ratio": ImageIssueCode.WRONG_ASPECT,
        "background": ImageIssueCode.BACKGROUND_OFF,
    }
    assert set(DEFAULT_SUGGESTIONS) == set(CHECK_CODES.values())
    assert DEFAULT_SUGGESTIONS[ImageIssueCode.BACKGROUND_OFF] == "Background removal / replacement"
    assert DEFAULT_SUGGESTIONS[ImageIssueCode.RESOLUTION_LOW] == "Upscale or re-export"
    assert DEFAULT_SUGGESTIONS[ImageIssueCode.WRONG_ASPECT] == "Crop to correct ratio"
    assert all(1 <= len(text) <= 1000 for text in DEFAULT_SUGGESTIONS.values())
    with pytest.raises(TypeError):
        CHECK_CODES["logo"] = ImageIssueCode.LOGO_PROHIBITED  # type: ignore[index]  # read-only map


# 6. EXIF orientation (measured dims checked through a 3:4 aspect rule)


def _portrait_rule() -> list[Any]:
    return [fx.image_rule("aspect_ratio", ratio_w=5, ratio_h=6)]


@pytest.mark.parametrize("orientation", [5, 6, 7, 8])
def test_exif_orientation_swaps_dimensions(orientation: int) -> None:
    report = run(fx.jpeg(1200, 1000, exif_orientation=orientation), _portrait_rule())
    assert report.pass_ is True
    assert ("exif_orientation", True) in [(c.check, c.passed) for c in report.checks]


@pytest.mark.parametrize("orientation", [1, 2, 3, 4, 0, 9, "x"])
def test_exif_orientation_ignored(orientation: object) -> None:
    report = run(fx.jpeg(1200, 1000, exif_orientation=orientation), _portrait_rule())
    assert codes(report) == ["WRONG_ASPECT"]
    assert ("exif_orientation", True) in [(c.check, c.passed) for c in report.checks]


def test_exif_orientation_malformed() -> None:
    img = fx.canvas(1200, 1000)
    data = fx.encode(img, "JPEG", exif=b"Exif\x00\x00MM\x00*\x00\x00\x00\x08\xff\xff\x01\x12")
    report = run(data, _portrait_rule())
    assert names(report) == [*INTRINSIC, "aspect_ratio"]
    assert codes(report) == ["WRONG_ASPECT"]


# 7. decoder restriction


@pytest.mark.parametrize(
    ("data", "fmt"),
    [(fx.jpeg(32, 32), "JPEG"), (fx.png(32, 32), "PNG"), (fx.tiff(32, 32), "TIFF")],
    ids=["jpeg", "png", "tiff"],
)
def test_decoder_restricted_to_sniffed_format(
    data: bytes, fmt: str, open_calls: list[dict[str, Any]]
) -> None:
    run(data)
    assert open_calls
    assert all(call.get("formats") == [fmt] for call in open_calls)


# 8. aware timestamps only


def test_naive_now_rejected() -> None:
    with pytest.raises(ValidationError):
        run(fx.jpeg(32, 32), now=datetime(2026, 10, 5, 12, 0))  # the point


# 9. JPEG scan walk


@pytest.mark.parametrize("n", [1, 3, 10])
def test_jpeg_scan_walk_counts(n: int) -> None:
    assert _jpeg_scan_count(fx.jpeg_scans(n)) == n


def test_jpeg_scan_walk_stuffing_and_restart() -> None:
    assert _jpeg_scan_count(fx.jpeg_scans(1, stuffing=True, restart=True)) == 1


def test_jpeg_scan_walk_skips_exif_thumbnail() -> None:
    data = fx.jpeg_with_exif_thumbnail()
    assert data.count(b"\xff\xda") >= 2
    assert _jpeg_scan_count(data) == 1
    assert run(data).pass_ is True


def test_jpeg_progressive_passes() -> None:
    data = fx.jpeg(256, 256, progressive=True)
    assert (_jpeg_scan_count(data) or 0) > 1
    assert run(data).pass_ is True


def _length_past_end() -> bytes:
    return b"\xff\xd8\xff\xe0" + struct.pack(">H", 4000) + b"JFIF\x00"


def test_jpeg_scan_walk_unwalkable_length_past_end(open_calls: list[dict[str, Any]]) -> None:
    assert _jpeg_scan_count(_length_past_end()) is None
    report = run(_length_past_end())
    assert names(report) == ["format", "corruption"]
    assert codes(report) == ["CORRUPT"]
    assert open_calls == []


def test_jpeg_scan_walk_unwalkable_no_soi() -> None:
    assert _jpeg_scan_count(b"\x00\xd8\xff\xe0\x00\x10") is None
    assert _jpeg_scan_count(b"") is None


# 10. decode boundary

MAPPED_CORRUPT = [
    TypeError,
    IndexError,
    KeyError,
    ZeroDivisionError,
    OverflowError,
    RecursionError,
    OSError,
    SyntaxError,
    struct.error,
]
MAPPED_TOO_LARGE = [MemoryError, Image.DecompressionBombError, Image.DecompressionBombWarning]


def _raise(exc: type[BaseException]) -> Callable[..., Any]:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise exc("seeded")

    return boom


@pytest.mark.parametrize("method", ["load", "verify"])
@pytest.mark.parametrize("exc", MAPPED_CORRUPT, ids=lambda e: e.__name__)
def test_decode_boundary_maps_corrupt(
    exc: type[Exception], method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ImageFile.ImageFile, method, _raise(exc))
    report = run(fx.jpeg(64, 64))
    assert names(report) == ["format", "corruption"]
    assert codes(report) == ["CORRUPT"]


@pytest.mark.parametrize("method", ["load", "verify"])
@pytest.mark.parametrize("exc", MAPPED_TOO_LARGE, ids=lambda e: e.__name__)
def test_decode_boundary_maps_too_large(
    exc: type[Exception], method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ImageFile.ImageFile, method, _raise(exc))
    report = run(fx.jpeg(64, 64))
    assert names(report) == ["format", "file_size"]
    assert codes(report) == ["FILE_TOO_LARGE"]


def test_decode_boundary_keyboard_interrupt_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ImageFile.ImageFile, "load", _raise(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        run(fx.jpeg(64, 64))


# 11. EXIF boundary


@pytest.mark.parametrize("exc", [TypeError, KeyError, ValueError], ids=lambda e: e.__name__)
def test_exif_boundary_maps_to_orientation_1(
    exc: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Image.Image, "getexif", _raise(exc))
    report = run(fx.jpeg(1200, 1000, exif_orientation=6), _portrait_rule())
    assert ("exif_orientation", True) in [(c.check, c.passed) for c in report.checks]
    assert codes(report) == ["WRONG_ASPECT"]


def test_module_keeps_no_logger() -> None:
    source = Path(image_checks.__file__).read_text("utf-8")
    assert "logging" not in source and "print(" not in source
