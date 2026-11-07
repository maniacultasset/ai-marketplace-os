"""SI7 T8: the candidate child and its runner (AC5-AC8, AC13). The real child runs every op on
synthetic images; the fakes prove that a hang, a crash, a flood or a bad reply is an
infrastructure failure, never a verdict (CP-D19)."""

from __future__ import annotations

import base64
import io
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.services import image_candidate_child, image_runner
from app.services.image_runner import (
    CandidateChildFailed,
    CandidateReply,
    parse_derived,
    run_candidate_isolated,
)
from mkos_schemas.enums import ImageSlot, Marketplace
from mkos_schemas.images import ImageCheck, ImageReport
from PIL import Image, ImageDraw
from tests import image_fixtures as fx

FAKES = "tests.support.image_candidate_fakes"
NOW = datetime(2026, 11, 7, tzinfo=UTC)
CANDIDATE = uuid.UUID("00000000-0000-4000-8000-0000000000c1")
PRODUCT = (40, 70, 160)


def _params(**extra: Any) -> dict[str, Any]:
    rules = {"amazon": [r.model_dump(mode="json", by_alias=True) for r in fx.amazon_rules()]}
    return {"rules": rules, "image_id": str(CANDIDATE), "now": NOW.isoformat(), **extra}


def _scene(bg: tuple[int, int, int], size: tuple[int, int] = (1500, 2000)) -> Image.Image:
    img = Image.new("RGB", size, bg)
    w, h = size
    ImageDraw.Draw(img).rectangle((w // 4, h // 4, 3 * w // 4, 3 * h // 4), fill=PRODUCT)
    return img


def _open(reply: CandidateReply) -> Image.Image:
    assert reply.ok and reply.image is not None
    img = Image.open(io.BytesIO(reply.image))
    assert img.format == "JPEG" and len(img.getexif()) == 0
    return img


async def test_prepare_pads_to_a_square_model_input() -> None:
    vision = fx.encode(_scene((245, 245, 245), (600, 800)), "JPEG", quality=90)
    reply = await run_candidate_isolated("prepare", {"vision": vision}, {})
    img = _open(reply)
    assert img.width == img.height == 800
    assert reply.border is not None and reply.border["near_white"] is True


async def test_fix_frames_an_off_white_photo() -> None:
    clean = fx.encode(_scene((245, 245, 245)), "PNG")
    reply = await run_candidate_isolated("fix", {"clean": clean}, _params())
    img = _open(reply)
    assert img.width == img.height >= 1000 and reply.width == img.width


async def test_fix_too_small_is_a_verdict() -> None:
    clean = fx.encode(_scene((245, 245, 245), (500, 500)), "PNG")
    reply = await run_candidate_isolated("fix", {"clean": clean}, _params())
    assert reply == CandidateReply(ok=False, reason="too_small")


async def test_finish_white_keeps_a_faithful_edit() -> None:
    clean_img = _scene((235, 235, 235), (2000, 2000))
    edit = _scene((255, 255, 255), (1024, 1024))
    reply = await run_candidate_isolated(
        "finish_white",
        {"clean": fx.encode(clean_img, "PNG"), "output": fx.encode(edit, "PNG")},
        _params(),
    )
    img = _open(reply)
    assert img.width == img.height >= 1000
    assert reply.fidelity is not None
    agreement = reply.fidelity["agreement"]
    assert agreement is not None and agreement >= 0.9


async def test_finish_white_moved_product_is_fidelity() -> None:
    clean_img = _scene((235, 235, 235), (2000, 2000))
    moved = Image.new("RGB", (1024, 1024), (255, 255, 255))
    ImageDraw.Draw(moved).rectangle((0, 0, 400, 400), fill=PRODUCT)
    reply = await run_candidate_isolated(
        "finish_white",
        {"clean": fx.encode(clean_img, "PNG"), "output": fx.encode(moved, "PNG")},
        _params(),
    )
    assert not reply.ok and reply.reason == "fidelity" and reply.fidelity is not None


@pytest.mark.parametrize("fmt", ["GIF", "TIFF"])
async def test_disallowed_model_output_is_unusable(fmt: str) -> None:
    clean = fx.encode(_scene((235, 235, 235), (2000, 2000)), "PNG")
    output = fx.encode(_scene((255, 255, 255), (1024, 1024)), fmt)
    reply = await run_candidate_isolated(
        "finish_white", {"clean": clean, "output": output}, _params()
    )
    assert reply == CandidateReply(ok=False, reason="source_unusable")


async def test_header_bomb_is_unusable_fast() -> None:
    clean = fx.encode(_scene((235, 235, 235), (2000, 2000)), "PNG")
    bomb = fx.png_header_only(30000, 30000)
    reply = await run_candidate_isolated(
        "finish_white", {"clean": clean, "output": bomb}, _params()
    )
    assert reply == CandidateReply(ok=False, reason="source_unusable")


async def test_size_chart_renders() -> None:
    chart = {"unit": "cm", "basis": "garment", "rows": [{"size": "M", "chest": 100}]}
    img = _open(await run_candidate_isolated("size_chart", {}, {"chart": chart, "fibres": None}))
    assert img.size == (1500, 1500)


@pytest.fixture
def fake_child(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr(image_runner, "CANDIDATE_MODULE", FAKES)
    return monkeypatch


@pytest.mark.parametrize(
    ("fake", "reason"),
    [("crash", "exit_3"), ("garbage", "bad_reply"), ("bad_reason", "bad_reply")],
)
async def test_infrastructure_failures_raise(
    fake_child: pytest.MonkeyPatch, fake: str, reason: str
) -> None:
    with pytest.raises(CandidateChildFailed) as info:
        await run_candidate_isolated("fix", {"clean": b"x"}, {"fake": fake})
    assert info.value.reason == reason


async def test_hang_is_killed_and_raises(fake_child: pytest.MonkeyPatch) -> None:
    fake_child.setattr(image_runner, "CANDIDATE_TIMEOUT_S", 1)
    with pytest.raises(CandidateChildFailed) as info:
        await run_candidate_isolated("fix", {"clean": b"x"}, {"fake": "hang"})
    assert info.value.reason == "timeout"


async def test_flood_is_capped_and_raises(fake_child: pytest.MonkeyPatch) -> None:
    fake_child.setattr(image_runner, "CANDIDATE_STDOUT_CAP", 1_000_000)
    with pytest.raises(CandidateChildFailed) as info:
        await run_candidate_isolated("fix", {"clean": b"x"}, {"fake": "flood"})
    assert info.value.reason == "over_cap"


def _report(passed_background: bool) -> ImageReport:
    checks = [
        ImageCheck(check="resolution", method="deterministic", passed=True),
        ImageCheck(check="background", method="deterministic", passed=passed_background),
    ]
    return ImageReport(
        image_id=CANDIDATE,
        marketplace=Marketplace.AMAZON,
        slot=ImageSlot.MAIN,
        pass_=passed_background,
        checks=checks,
        processed_at=NOW,
    )


def _child(op: str, inputs: dict[str, bytes], params: dict[str, Any]) -> dict[str, Any]:
    request = {
        "op": op,
        "inputs": {k: base64.b64encode(v).decode() for k, v in inputs.items()},
        "params": params,
    }
    out: dict[str, Any] = json.loads(image_candidate_child.run(json.dumps(request).encode()))
    return out


def test_fix_output_failing_background_is_never_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_candidate_child, "check_image", lambda *a, **k: _report(False))
    clean = fx.encode(_scene((245, 245, 245)), "PNG")
    assert _child("fix", {"clean": clean}, _params()) == {"ok": False, "reason": "not_white"}
    monkeypatch.setattr(image_candidate_child, "check_image", lambda *a, **k: _report(True))
    assert _child("fix", {"clean": clean}, _params())["ok"] is True


def test_bad_request_is_not_a_verdict() -> None:
    with pytest.raises(image_candidate_child.BadRequest):
        image_candidate_child.run(b'{"op":"lifestyle"}')
    with pytest.raises(image_candidate_child.BadRequest):
        image_candidate_child.run(b'{"op":"fix","inputs":{"clean":"!!"}}')


async def test_no_base64_reaches_the_logs(
    caplog: pytest.LogCaptureFixture, fake_child: pytest.MonkeyPatch
) -> None:
    clean = fx.encode(_scene((245, 245, 245)), "PNG")
    marker = base64.b64encode(clean)[:64].decode()
    with caplog.at_level(logging.DEBUG), pytest.raises(CandidateChildFailed):
        await run_candidate_isolated("fix", {"clean": clean}, {"fake": "crash"})
    assert marker not in caplog.text and "CANARY-STDERR" not in caplog.text


def test_parse_derived_with_and_without_border() -> None:
    jpeg = fx.jpeg(8, 8)
    body = {
        "clean_b64": base64.b64encode(jpeg).decode(),
        "clean_mime": "image/jpeg",
        "vision_b64": base64.b64encode(jpeg).decode(),
        "vision_w": 8,
        "vision_h": 8,
    }
    old = parse_derived(json.dumps(body).encode())
    assert old is not None and old.border is None
    border = {"uniform": True, "near_white": False, "colour": [200, 200, 200]}
    new = parse_derived(json.dumps({**body, "border": border}).encode())
    assert new is not None and new.border == border
    bad = parse_derived(json.dumps({**body, "border": {"uniform": "yes"}}).encode())
    assert bad is not None and bad.border is None


async def test_finish_white_whitens_an_off_white_echo() -> None:
    """The stub echoes the off-white input: the backend's own whitening makes it pass."""
    clean_img = _scene((245, 245, 245), (2000, 2000))
    echo = _scene((245, 245, 245), (1024, 1024))
    reply = await run_candidate_isolated(
        "finish_white",
        {"clean": fx.encode(clean_img, "PNG"), "output": fx.encode(echo, "JPEG", quality=90)},
        _params(),
    )
    assert reply.ok, reply.reason


async def test_finish_white_grey_echo_is_not_white() -> None:
    clean_img = _scene((200, 200, 200), (2000, 2000))
    echo = _scene((200, 200, 200), (1024, 1024))
    reply = await run_candidate_isolated(
        "finish_white",
        {"clean": fx.encode(clean_img, "PNG"), "output": fx.encode(echo, "JPEG", quality=90)},
        _params(),
    )
    assert not reply.ok and reply.reason == "not_white"


async def test_finish_white_whitens_a_240_echo_before_judging() -> None:
    clean_img = _scene((240, 240, 240), (2000, 2000))
    echo = _scene((240, 240, 240), (1024, 1024))
    reply = await run_candidate_isolated(
        "finish_white",
        {"clean": fx.encode(clean_img, "PNG"), "output": fx.encode(echo, "JPEG", quality=90)},
        _params(),
    )
    assert reply.ok, (reply.reason, reply.fidelity)
