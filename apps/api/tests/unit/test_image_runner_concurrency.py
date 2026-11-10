"""V1 hardening #9: the image children run under a per-process limit of IMAGE_CHECK_CONCURRENCY
(default 1), one semaphore per event loop."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.core.config import Settings, get_settings
from app.services import image_runner
from pydantic import ValidationError


async def _held_together(limit_env: str | None, monkeypatch: pytest.MonkeyPatch) -> int:
    if limit_env is not None:
        monkeypatch.setenv("IMAGE_CHECK_CONCURRENCY", limit_env)
    get_settings.cache_clear()
    sem = image_runner._semaphore()
    assert image_runner._semaphore() is sem  # one per loop
    running = peak = 0

    async def child() -> None:
        nonlocal running, peak
        async with image_runner._semaphore():
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

    await asyncio.gather(*(child() for _ in range(6)))
    return peak


async def test_default_is_one_child(
    ci_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await _held_together(None, monkeypatch) == 1


async def test_configured_children_run_together(
    ci_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await _held_together("2", monkeypatch) == 2


@pytest.mark.parametrize("value", ["0", "5"])
def test_limit_is_bounded(clean_env: Path, value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="local",
            database_url="postgresql+psycopg://u:p@localhost:5432/x",
            jwt_secret="ci_only_not_a_real_secret_at_least_32_chars",
            image_check_concurrency=value,
        )
