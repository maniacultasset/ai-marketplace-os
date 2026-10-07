"""S13 A14: the hook type aliases later steps cite (S16, S18, S21) stay exactly these."""

from __future__ import annotations

import typing
from collections.abc import Awaitable

from app.db.models.ops import JobRun
from app.services import job_hooks
from app.workers.registry import Outcome


def test_hook_type_aliases_literal() -> None:
    hook_args, hook_ret = typing.get_args(job_hooks.Hook)
    assert hook_args == [typing.Any, JobRun, Outcome | None]
    assert typing.get_origin(hook_ret) is Awaitable and typing.get_args(hook_ret) == (None,)
    requeue_args, requeue_ret = typing.get_args(job_hooks.RequeueHook)
    assert requeue_args == [typing.Any, JobRun, JobRun]
    assert typing.get_args(requeue_ret) == (None,)


def test_hook_maps_start_empty() -> None:
    assert job_hooks.TERMINAL_HOOKS == {}
    assert job_hooks.REQUEUE_HOOKS == {}
