"""SI7 seam models: ImageEditRequest, ImageEditResult, EditImage (AC1, AC13)."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import typing
from pathlib import Path
from typing import Any

import mkos_schemas.pipeline as pipeline_mod
import pytest
from mkos_schemas.image_edit import MAX_EDIT_IMAGE_B64_CHARS, ImageEditRequest, ImageEditResult
from mkos_schemas.jobs import ModelCredentials
from mkos_schemas.pipeline import EditImage, PipelineDeps, edit_image
from pydantic import ValidationError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _fixture(name: str) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return raw


_MAGIC = {
    "PNG": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
    "JPEG": b"\xff\xd8\xff\xe0\x00\x10JFIF\x00",
    "WEBP": b"RIFF\x24\x00\x00\x00WEBPVP8 ",
    "GIF": b"GIF89a\x01\x00\x01\x00",
}


def _b64(fmt: str, size: tuple[int, int] = (2, 2)) -> str:
    """Magic bytes plus filler: the seam checks the magic only; pixels are the backend's job."""
    return base64.b64encode(_MAGIC[fmt] + b"\x00" * (size[0] * size[1])).decode()


def _ok(**changes: Any) -> dict[str, Any]:
    raw = copy.deepcopy(_fixture("ImageEditResult"))
    raw.update(changes)
    return raw


def _failed(**changes: Any) -> dict[str, Any]:
    error = _fixture("ErrorReport")
    raw: dict[str, Any] = {"status": "failed", "error": error, "agent_runs": []}
    raw.update(changes)
    return raw


def test_fixtures_round_trip() -> None:
    for name, model in (
        ("ImageEditRequest", ImageEditRequest),
        ("ImageEditResult", ImageEditResult),
    ):
        raw = _fixture(name)
        assert model.model_validate(raw).model_dump(mode="json") == raw


@pytest.mark.parametrize(
    "raw",
    [
        _ok(image_b64=None),
        _ok(mime=None),
        _ok(agent_runs=[]),
        _ok(agent_runs=_fixture("ImageEditResult")["agent_runs"] * 2),
        _ok(error=_fixture("ErrorReport")),
        _failed(error=None),
        _failed(image_b64=_b64("PNG"), mime="image/png"),
        _failed(mime="image/png"),
    ],
    ids=[
        "ok-no-image",
        "ok-no-mime",
        "ok-0-runs",
        "ok-2-runs",
        "ok-with-error",
        "failed-no-error",
        "failed-with-image",
        "failed-with-mime",
    ],
)
def test_status_shape_refused(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ImageEditResult.model_validate(raw)


def test_failed_with_one_run_is_accepted() -> None:
    runs = _fixture("ImageEditResult")["agent_runs"]
    result = ImageEditResult.model_validate(_failed(agent_runs=runs))
    assert result.status == "failed" and len(result.agent_runs) == 1


@pytest.mark.parametrize(
    "value",
    ["", "!!!", _b64("GIF"), "A" * (MAX_EDIT_IMAGE_B64_CHARS + 1), 7],
    ids=["empty", "junk", "gif", "over-cap", "not-a-string"],
)
def test_image_b64_refused(value: object) -> None:
    with pytest.raises(ValidationError):
        ImageEditResult.model_validate(_ok(image_b64=value))


@pytest.mark.parametrize(
    ("fmt", "mime"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
def test_image_b64_accepted(fmt: str, mime: str) -> None:
    result = ImageEditResult.model_validate(_ok(image_b64=_b64(fmt), mime=mime))
    assert result.image_b64 is not None


def test_image_b64_with_trailing_padding_is_refused() -> None:
    png = _b64("PNG")
    padded = png + "=" * 4
    with pytest.raises(ValidationError):
        ImageEditResult.model_validate(_ok(image_b64=padded))


def test_base64_never_reaches_repr_errors_or_dumps() -> None:
    png = _b64("PNG", (64, 64))
    result = ImageEditResult.model_validate(_ok(image_b64=png))
    assert png not in repr(result)
    assert png not in str(result)
    bad = _ok(image_b64=png, error=_fixture("ErrorReport"))
    with pytest.raises(ValidationError) as caught:
        ImageEditResult.model_validate(bad)
    assert png not in str(caught.value)
    assert png[:40] not in str(caught.value)
    gif = _b64("GIF", (64, 64))
    with pytest.raises(ValidationError) as caught_gif:
        ImageEditResult.model_validate(_ok(image_b64=gif))
    assert gif not in str(caught_gif.value)


def test_request_base64_never_reaches_repr_or_errors() -> None:
    raw = _fixture("ImageEditRequest")
    request = ImageEditRequest.model_validate(raw)
    data = raw["image"]["data_b64"]
    assert data not in repr(request)
    bad = copy.deepcopy(raw)
    bad["category"] = "x" * 65
    with pytest.raises(ValidationError) as caught:
        ImageEditRequest.model_validate(bad)
    assert data not in str(caught.value)


def test_request_task_and_category() -> None:
    raw = _fixture("ImageEditRequest")
    for change in ({"task": "lifestyle"}, {"category": "x" * 65}, {"category": "  "}):
        bad = copy.deepcopy(raw)
        bad.update(change)
        with pytest.raises(ValidationError):
            ImageEditRequest.model_validate(bad)
    unicode = copy.deepcopy(raw)
    unicode["category"] = "कुर्ता"
    assert ImageEditRequest.model_validate(unicode).category == "कुर्ता"


def test_request_attempt_bounds() -> None:
    raw = _fixture("ImageEditRequest")
    for attempt in (0, 4):
        bad = copy.deepcopy(raw)
        bad["attempt"] = attempt
        with pytest.raises(ValidationError):
            ImageEditRequest.model_validate(bad)


def test_served_provider() -> None:
    assert ImageEditResult.model_validate(_ok(served_provider=None)).served_provider is None
    without = _ok()
    del without["served_provider"]
    assert ImageEditResult.model_validate(without).served_provider is None
    assert ImageEditResult.model_validate(_ok(served_provider="Google")).served_provider == "Google"
    for bad in (" ", "x" * 65):
        with pytest.raises(ValidationError):
            ImageEditResult.model_validate(_ok(served_provider=bad))


def test_closed_literals_unchanged() -> None:
    provider = ModelCredentials.model_fields["provider"].annotation
    assert set(typing.get_args(provider)) == {"openai", "deepseek"}
    hints = typing.get_type_hints(PipelineDeps.get_model_credentials)
    assert set(typing.get_args(hints["purpose"])) == {"generation", "vision", "embedding"}


def test_edit_image_declares_the_contract_only() -> None:
    request = ImageEditRequest.model_validate(_fixture("ImageEditRequest"))
    creds = ModelCredentials(provider="openai", api_key="x-test", model="m")
    with pytest.raises(NotImplementedError):
        asyncio.run(edit_image(request, creds))
    implementation: EditImage = edit_image
    assert callable(implementation)
    assert "edit_image" in pipeline_mod.__all__
