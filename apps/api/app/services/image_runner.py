"""Run the image checks in a killable child process (S17b PLAN D6, CP6; S17a hand-off).

`run_checks_isolated(data, image_id=, slot=, rules=, now=)` starts `app.services.image_check_child`
with an environment allow-list, under a per-process semaphore of one (a backstop: the queue's
`image_compliance` lock already keeps a second image job unfetched, PLAN D15(b)), and waits at
most `CHILD_TIMEOUT_S`. It never raises for a hostile image:

- a timeout (the child is killed and reaped), a non-zero exit, a signal, or stdout that does not
  hold one valid report per requested marketplace → one `pass=false` CORRUPT report per
  marketplace and no meta (deterministic, never retried);
- exit 4 (a misconfigured rule) → `JobFailed(INTERNAL_ERROR)`, never a CORRUPT report;
- a failure to start the child (`OSError`) → `RetryableJobError(INTERNAL_ERROR)`.

The child's stderr is discarded, never logged: it may quote decoder text about the image.

SI1 T15 (CP17): `run_derive_isolated(data, image_id=)` runs `app.services.image_derive_child`, a
second child with its own limits and timeout, after the checks and under the same semaphore. Its
stdout is read through a bounded reader (`DERIVE_STDOUT_CAP`), never `communicate()`. Over the
cap, a timeout (killed), a non-zero exit, bad JSON or base64, a wrong mime type or bad sizes →
None and one log line by image id: it never returns a CORRUPT report and never raises, except
`OSError` at spawn → `RetryableJobError`, as for the checks.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import sys
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, ImageSlot, Marketplace
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.images import ImageCheck, ImageIssue, ImageReport
from mkos_schemas.rules import Rule
from pydantic import ValidationError

from app.core.config import child_process_env
from app.core.logging import get_logger
from app.repositories.images import ImageMeta
from app.services.image_checks import CHECK_CODES, DEFAULT_SUGGESTIONS
from app.workers.errors import JobFailed, RetryableJobError

CHILD_TIMEOUT_S: Final = 60
CHILD_MODULE: Final = "app.services.image_check_child"
DERIVE_MODULE: Final = "app.services.image_derive_child"
DERIVE_TIMEOUT_S: Final = 60
MAX_IMAGE_BYTES: Final = 10_485_760
VISION_MAX_PX: Final = 1024
# two base64 copies at most (clean ≤ 10 MiB, vision ≤ 1.5 MiB) plus the JSON around them
DERIVE_STDOUT_CAP: Final = 2 * MAX_IMAGE_BYTES * 4 // 3 + 1_048_576
_READ_CHUNK: Final = 65_536
_MAGIC: Final = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n"}
log = get_logger(__name__)
EXIT_RULE_MISCONFIGURED: Final = 4
# apps/api: the child's cwd, so `-m app.services...` resolves to this checkout's package
API_ROOT: Final = Path(__file__).resolve().parents[2]
_SEMAPHORES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


@dataclass(frozen=True, slots=True)
class DerivedImages:
    clean: bytes
    clean_mime: str
    vision: bytes
    vision_w: int
    vision_h: int
    border: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    reports: dict[Marketplace, ImageReport]
    meta: ImageMeta | None


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    found = _SEMAPHORES.get(loop)
    if found is None:
        found = _SEMAPHORES[loop] = asyncio.Semaphore(1)
    return found


def child_env() -> dict[str, str]:
    """The allow-listed environment: no DSN, token, key or secret reaches the decoder."""
    env = child_process_env()
    env["LANG"] = "C.UTF-8"
    env["PYTHONHASHSEED"] = "0"
    return env


def corrupt_reports(
    image_id: UUID, slot: ImageSlot, marketplaces: Sequence[Marketplace], now: datetime
) -> dict[Marketplace, ImageReport]:
    """One failed `corruption` report per marketplace (the S17a default suggestion)."""
    code = CHECK_CODES["corruption"]
    return {
        m: ImageReport(
            image_id=image_id,
            marketplace=m,
            slot=slot,
            pass_=False,
            checks=[ImageCheck(check="corruption", method="deterministic", passed=False)],
            issues=[
                ImageIssue(code=code, check="corruption", suggestion=DEFAULT_SUGGESTIONS[code])
            ],
            processed_at=now,
        )
        for m in marketplaces
    }


def _misconfigured() -> JobFailed:
    item = ErrorItem(
        field="request",
        error_code=ErrorCode.INTERNAL_ERROR,
        message="image rule misconfigured",
        source=ErrorSource.SYSTEM,
        retryable=False,
    )
    return JobFailed(
        ErrorReport(
            status="failed", error_code=ErrorCode.INTERNAL_ERROR, retryable=False, errors=[item]
        )
    )


def _parse(
    stdout: bytes, marketplaces: Sequence[Marketplace], image_id: UUID
) -> CheckOutcome | None:
    try:
        body = json.loads(stdout)
        raw: Mapping[str, Any] = body["reports"]
        reports = {m: ImageReport.model_validate(raw[m.value]) for m in marketplaces}
        meta_raw = body.get("meta")
        meta = None if meta_raw is None else ImageMeta(**meta_raw)
    except (ValueError, KeyError, TypeError, ValidationError):
        return None
    if any(r.image_id != image_id or r.marketplace is not m for m, r in reports.items()):
        return None
    return CheckOutcome(reports=reports, meta=meta)


async def run_checks_isolated(
    data: bytes,
    *,
    image_id: UUID,
    slot: ImageSlot,
    rules: Mapping[Marketplace, Sequence[Rule]],
    now: datetime,
) -> CheckOutcome:
    marketplaces = list(rules)
    header = {
        "image_id": str(image_id),
        "slot": ImageSlot(slot).value,
        "now": now.isoformat(),
        "rules": {
            Marketplace(m).value: [r.model_dump(mode="json", by_alias=True) for r in rs]
            for m, rs in rules.items()
        },
    }
    stdin = json.dumps(header, separators=(",", ":")).encode("utf-8") + b"\n" + data
    async with _semaphore():
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                CHILD_MODULE,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=API_ROOT,
                env=child_env(),
            )
        except OSError:
            raise RetryableJobError(ErrorCode.INTERNAL_ERROR) from None
        try:
            async with asyncio.timeout(CHILD_TIMEOUT_S):
                stdout, _ = await proc.communicate(stdin)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return CheckOutcome(corrupt_reports(image_id, slot, marketplaces, now), None)
        finally:
            if proc.returncode is None:  # cancelled while waiting: never leave the child running
                proc.kill()
                await proc.wait()
    if proc.returncode == EXIT_RULE_MISCONFIGURED:
        raise _misconfigured()
    parsed = _parse(stdout, marketplaces, image_id) if proc.returncode == 0 else None
    if parsed is None:
        return CheckOutcome(corrupt_reports(image_id, slot, marketplaces, now), None)
    return parsed


# ---- SI1 T15: the derive child ----


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> bytes | None:
    """Read to EOF; None as soon as more than `cap` bytes arrived (never holds more)."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > cap:
            return None
        chunks.append(chunk)


async def _feed(proc: Any, data: bytes) -> None:
    assert proc.stdin is not None  # noqa: S101 - spawned with stdin=PIPE
    try:
        proc.stdin.write(data)
        await proc.stdin.drain()
        proc.stdin.close()
    except (BrokenPipeError, ConnectionResetError):
        pass  # the child exited early; its exit code decides


def _b64(value: Any) -> bytes | None:
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


def _px(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= VISION_MAX_PX


def parse_derived(stdout: bytes) -> DerivedImages | None:
    """The child's JSON as `DerivedImages`, or None when it is `null` or not well formed."""
    try:
        body = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    clean, vision = _b64(body.get("clean_b64")), _b64(body.get("vision_b64"))
    mime, w, h = body.get("clean_mime"), body.get("vision_w"), body.get("vision_h")
    if clean is None or vision is None or mime not in _MAGIC:
        return None
    if not clean.startswith(_MAGIC[mime]) or not vision.startswith(_MAGIC["image/jpeg"]):
        return None
    if len(clean) > MAX_IMAGE_BYTES:
        return None
    if not (_px(w) and _px(h)):
        return None
    assert isinstance(w, int) and isinstance(h, int)  # noqa: S101 - narrowed by _px
    return DerivedImages(
        clean=clean,
        clean_mime=mime,
        vision=vision,
        vision_w=w,
        vision_h=h,
        border=parse_border(body.get("border")),
    )


def parse_border(value: Any) -> dict[str, Any] | None:
    """SI7: a child's border profile `{uniform, near_white, colour}`, or None (an older child, or a
    malformed value: the product read then counts the border as not near-white)."""
    if not isinstance(value, dict):
        return None
    uniform, near_white, colour = value.get("uniform"), value.get("near_white"), value.get("colour")
    if not isinstance(uniform, bool) or not isinstance(near_white, bool):
        return None
    if not isinstance(colour, list) or len(colour) != 3:
        return None
    if not all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255 for c in colour):
        return None
    return {"uniform": uniform, "near_white": near_white, "colour": list(colour)}


async def run_derive_isolated(data: bytes, *, image_id: UUID) -> DerivedImages | None:
    async with _semaphore():
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                DERIVE_MODULE,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=API_ROOT,
                env=child_env(),
            )
        except OSError:
            raise RetryableJobError(ErrorCode.INTERNAL_ERROR) from None
        stdout: bytes | None = None
        reason = "ok"
        try:
            async with asyncio.timeout(DERIVE_TIMEOUT_S):
                assert proc.stdout is not None  # noqa: S101 - spawned with stdout=PIPE
                feeder = asyncio.create_task(_feed(proc, data))
                stdout = await _read_capped(proc.stdout, DERIVE_STDOUT_CAP)
                if stdout is None:
                    reason = "over_cap"
                    proc.kill()
                await feeder
                await proc.wait()
        except TimeoutError:
            reason = "timeout"
            stdout = None
        finally:
            if proc.returncode is None:  # timed out or cancelled: never leave the child running
                proc.kill()
                await proc.wait()
    if stdout is not None and proc.returncode != 0:
        reason, stdout = f"exit_{proc.returncode}", None
    found = None if stdout is None else parse_derived(stdout)
    if found is None:
        log.info(
            "image.derive_skipped",
            image_id=str(image_id),
            reason=reason if stdout is None else "unusable",
        )
    return found


# ---- SI7 T8: the candidate child ----

CANDIDATE_MODULE: Final = "app.services.image_candidate_child"
CANDIDATE_TIMEOUT_S: Final = 90
# one reply image (at most 10 MiB, base64) plus the JSON around it
CANDIDATE_STDOUT_CAP: Final = MAX_IMAGE_BYTES * 4 // 3 + 1_048_576
CANDIDATE_REASONS: Final = frozenset({"too_small", "source_unusable", "fidelity", "not_white"})
CANDIDATE_MAX_SIDE: Final = 20_000
_FIDELITY_KEYS: Final = ("mask_iou", "colour_distance", "agreement", "hole_share")


class CandidateChildFailed(Exception):  # noqa: N818 - CODE-PLAN name
    """The candidate child timed out, crashed, overflowed its stdout cap or replied badly: an
    infrastructure failure, never a verdict (CP-D19). `reason` is a fixed word for the log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class CandidateReply:
    ok: bool
    image: bytes | None = None
    width: int | None = None
    height: int | None = None
    fidelity: dict[str, float | None] | None = None
    border: dict[str, Any] | None = None
    reason: str | None = None


def _side(value: Any) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= CANDIDATE_MAX_SIDE
    )


def _fidelity(value: Any) -> dict[str, float | None] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(_FIDELITY_KEYS):
        raise CandidateChildFailed("bad_reply")
    out: dict[str, float | None] = {}
    for key in _FIDELITY_KEYS:
        number = value[key]
        if number is None and key == "mask_iou":
            out[key] = None
            continue
        if isinstance(number, bool) or not isinstance(number, int | float):
            raise CandidateChildFailed("bad_reply")
        if not 0.0 <= float(number) <= 1.0:
            raise CandidateChildFailed("bad_reply")
        out[key] = float(number)
    return out


def parse_candidate(stdout: bytes) -> CandidateReply:
    """The child's reply, or `CandidateChildFailed("bad_reply")`."""
    try:
        body = json.loads(stdout)
    except ValueError:
        raise CandidateChildFailed("bad_reply") from None
    if not isinstance(body, dict) or not isinstance(body.get("ok"), bool):
        raise CandidateChildFailed("bad_reply")
    fidelity = _fidelity(body.get("fidelity"))
    if body["ok"] is False:
        reason = body.get("reason")
        if reason not in CANDIDATE_REASONS:
            raise CandidateChildFailed("bad_reply")
        return CandidateReply(ok=False, reason=str(reason), fidelity=fidelity)
    image = _b64(body.get("image_b64"))
    width, height = body.get("width"), body.get("height")
    if image is None or not image.startswith(_MAGIC["image/jpeg"]) or len(image) > MAX_IMAGE_BYTES:
        raise CandidateChildFailed("bad_reply")
    if not (_side(width) and _side(height)):
        raise CandidateChildFailed("bad_reply")
    assert isinstance(width, int) and isinstance(height, int)  # noqa: S101 - narrowed by _side
    border = body.get("border")
    parsed_border = None if border is None else parse_border(border)
    if border is not None and parsed_border is None:
        raise CandidateChildFailed("bad_reply")
    return CandidateReply(
        ok=True,
        image=image,
        width=width,
        height=height,
        fidelity=fidelity,
        border=parsed_border,
    )


async def run_candidate_isolated(
    op: str, inputs: Mapping[str, bytes], params: Mapping[str, Any]
) -> CandidateReply:
    """Run one candidate op in the killable child under the image semaphore. A verdict comes back
    as `CandidateReply(ok=False, reason=...)`; every infrastructure failure raises
    `CandidateChildFailed` (an `OSError` at spawn is `RetryableJobError`, as for the checks).
    stdout and stderr never reach a log line."""
    request = {
        "op": op,
        "inputs": {name: base64.b64encode(data).decode("ascii") for name, data in inputs.items()},
        "params": dict(params),
    }
    payload = json.dumps(request, separators=(",", ":"), default=str).encode("ascii")
    async with _semaphore():
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                CANDIDATE_MODULE,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=API_ROOT,
                env=child_env(),
            )
        except OSError:
            raise RetryableJobError(ErrorCode.INTERNAL_ERROR) from None
        stdout: bytes | None = None
        try:
            async with asyncio.timeout(CANDIDATE_TIMEOUT_S):
                assert proc.stdout is not None  # noqa: S101 - spawned with stdout=PIPE
                feeder = asyncio.create_task(_feed(proc, payload))
                stdout = await _read_capped(proc.stdout, CANDIDATE_STDOUT_CAP)
                if stdout is None:
                    proc.kill()
                await feeder
                await proc.wait()
        except TimeoutError:
            raise CandidateChildFailed("timeout") from None
        finally:
            if proc.returncode is None:  # timed out or cancelled: never leave the child running
                proc.kill()
                await proc.wait()
    if stdout is None:
        raise CandidateChildFailed("over_cap")
    if proc.returncode != 0:
        raise CandidateChildFailed(f"exit_{proc.returncode}")
    return parse_candidate(stdout)
