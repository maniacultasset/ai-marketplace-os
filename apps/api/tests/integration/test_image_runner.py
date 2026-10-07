"""S17b T6 (PLAN D6, CP6; AC8): the checks run in a killable child with an env allow-list, at
concurrency one per process, and never raise for a hostile image."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from app.services import image_runner
from app.services.image_checks import check_image
from app.services.image_runner import CheckOutcome, run_checks_isolated
from app.workers.errors import JobFailed, RetryableJobError
from mkos_schemas.enums import ImageSlot, Marketplace
from mkos_schemas.rules import Rule
from tests.image_fixtures import NOW, amazon_rules, image_rule, jpeg, png, tiff, truncated

FAKES = "tests.support.image_child_fakes"
BOTH = (Marketplace.AMAZON, Marketplace.FLIPKART)


def rules_for(*markets: Marketplace) -> dict[Marketplace, list[Rule]]:
    out: dict[Marketplace, list[Rule]] = {}
    for m in markets:
        out[m] = [r.model_copy(update={"marketplace": m}) for r in amazon_rules()]
    return out


def fake_id(mode: int) -> uuid.UUID:
    return uuid.UUID(f"{mode:08x}-0000-4000-8000-{uuid.uuid4().hex[:12]}")


async def run(
    data: bytes,
    image_id: uuid.UUID | None = None,
    rules: Mapping[Marketplace, Sequence[Rule]] | None = None,
) -> CheckOutcome:
    return await run_checks_isolated(
        data,
        image_id=image_id or uuid.uuid4(),
        slot=ImageSlot.MAIN,
        rules=rules if rules is not None else rules_for(*BOTH),
        now=NOW,
    )


def is_corrupt(outcome: CheckOutcome, markets: Sequence[Marketplace] = BOTH) -> bool:
    return (
        set(outcome.reports) == set(markets)
        and outcome.meta is None
        and all(
            not r.pass_ and [i.code.value for i in r.issues] == ["CORRUPT"]
            for r in outcome.reports.values()
        )
    )


async def test_child_matches_in_process() -> None:
    data, image_id = jpeg(800, 800), uuid.uuid4()
    rules = rules_for(*BOTH)
    outcome = await run(data, image_id, rules)
    for m in BOTH:
        expected = check_image(
            data, image_id=image_id, marketplace=m, slot=ImageSlot.MAIN, rules=rules[m], now=NOW
        )
        assert outcome.reports[m] == expected


@pytest.mark.parametrize(
    ("fmt", "data", "mime"),
    [
        ("jpeg", jpeg(640, 480), "image/jpeg"),
        ("png", png(640, 480), "image/png"),
        ("tiff", tiff(640, 480), "image/tiff"),
    ],
)
async def test_child_meta_probe(fmt: str, data: bytes, mime: str) -> None:
    outcome = await run(data)
    assert outcome.meta is not None, fmt
    assert (outcome.meta.mime_type, outcome.meta.width_px, outcome.meta.height_px) == (
        mime,
        640,
        480,
    )


async def test_child_meta_none_for_corrupt() -> None:
    outcome = await run(truncated(jpeg(640, 480), 200))
    assert outcome.meta is None
    assert all(not r.pass_ for r in outcome.reports.values())


@pytest.fixture
def fake_child(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr(image_runner, "CHILD_MODULE", FAKES)
    return monkeypatch


async def test_child_timeout_killed_corrupt(fake_child: pytest.MonkeyPatch) -> None:
    fake_child.setattr(image_runner, "CHILD_TIMEOUT_S", 1)
    procs: list[Any] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args: Any, **kwargs: Any) -> Any:
        proc = await real(*args, **kwargs)
        procs.append(proc)
        return proc

    fake_child.setattr(asyncio, "create_subprocess_exec", spy)
    started = time.monotonic()
    outcome = await run(b"x", fake_id(1))
    assert time.monotonic() - started < 10
    assert is_corrupt(outcome)
    assert procs and procs[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(procs[0].pid, 0)


async def test_child_crash_corrupt(fake_child: pytest.MonkeyPatch) -> None:
    assert is_corrupt(await run(b"x", fake_id(2)))


async def test_child_garbage_stdout_corrupt(fake_child: pytest.MonkeyPatch) -> None:
    assert is_corrupt(await run(b"x", fake_id(3)))


async def test_child_missing_marketplace_corrupt(fake_child: pytest.MonkeyPatch) -> None:
    assert is_corrupt(await run(b"x", fake_id(6)))


async def test_child_env_has_no_secrets(
    fake_child: pytest.MonkeyPatch,
) -> None:
    """The env holds only the allow-list (it is not a credential sandbox: CP6)."""
    for name in (
        "DATABASE_URL",
        "JWT_SECRET",
        "S3_SECRET_KEY",
        "KMS_LOCAL_DEV_KEY",
        "ADMIN_SESSION_SECRET",
    ):
        fake_child.setenv(name, "canary-secret-value")
    seen: list[bytes] = []
    fake_child.setattr(image_runner, "_parse", lambda out, *_a: seen.append(out))
    await run(b"x", fake_id(4))
    import json

    keys = set(json.loads(seen[0]))
    assert keys <= {
        "PATH",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "LANG",
        "PYTHONHASHSEED",
        "LC_CTYPE",
        "__CF_USER_TEXT_ENCODING",
    }
    assert {"LANG", "PYTHONHASHSEED"} <= keys


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_AS not enforced on darwin")
def test_child_rlimits_set() -> None:
    import resource

    from app.services import image_check_child

    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child process
        image_check_child.set_limits()
        ok = resource.getrlimit(resource.RLIMIT_AS)[0] == image_check_child.AS_LIMIT_BYTES
        ok = ok and resource.getrlimit(resource.RLIMIT_CPU)[0] == image_check_child.CPU_LIMIT_S
        os._exit(0 if ok else 1)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0


async def test_runner_concurrency_one(fake_child: pytest.MonkeyPatch) -> None:
    started = time.monotonic()
    first, second = await asyncio.gather(run(b"x", fake_id(5)), run(b"x", fake_id(5)))
    assert time.monotonic() - started >= 1.0
    assert is_corrupt(first) and is_corrupt(second)


async def test_spawn_failure_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(*_a: Any, **_k: Any) -> Any:
        raise OSError("no fork")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)
    with pytest.raises(RetryableJobError):
        await run(b"x")


async def test_child_misconfigured_rule_job_failed() -> None:
    bad = image_rule("background", min_px=10)  # a background rule without rgb: a seed bug
    with pytest.raises(JobFailed) as caught:
        await run(jpeg(800, 800), rules={Marketplace.AMAZON: [bad]})
    assert caught.value.report.errors[0].message == "image rule misconfigured"


async def test_child_stderr_not_logged(
    fake_child: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    await run(b"x", fake_id(2))
    out, err = capfd.readouterr()
    assert "CANARY" not in caplog.text + out + err
