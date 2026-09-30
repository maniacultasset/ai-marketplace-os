"""AC12, AC15 (added by the e2e tester): the commit-lint and gitleaks jobs read the real PR and
push values. A constant PR_TITLE, HEAD_REF or BASE_SHA would keep the required check green on
every PR while checking nothing (e.g. BASE_SHA = head.sha scans an empty range)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

EXPECTED_ENV = [
    (
        "ci-meta.yml",
        "commit-lint",
        {
            "PR_TITLE": "${{ github.event.pull_request.title }}",
            "HEAD_REF": "${{ github.head_ref }}",
        },
    ),
    (
        "ci-meta.yml",
        "gitleaks",
        {
            "BASE_SHA": "${{ github.event.pull_request.base.sha }}",
            "HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
        },
    ),
    (
        "main-gitleaks.yml",
        "gitleaks-main",
        {
            "BASE_SHA": "${{ github.event.before }}",
            "HEAD_SHA": "${{ github.event.after }}",
        },
    ),
]


def _make_step_env(job: dict[str, Any]) -> dict[str, Any]:
    steps = [step for step in job["steps"] if str(step.get("run", "")).startswith("make ")]
    assert len(steps) == 1, "exactly one `make <check-id>` step"
    env = steps[0].get("env")
    assert isinstance(env, dict)
    return env


@pytest.mark.parametrize(("workflow", "job_id", "expected"), EXPECTED_ENV)
def test_check_reads_the_event_values(
    repo_root: Path,
    load_yaml: Callable[[Path], Any],
    workflow: str,
    job_id: str,
    expected: dict[str, str],
) -> None:
    data = load_yaml(repo_root / ".github/workflows" / workflow)
    env = _make_step_env(data["jobs"][job_id])
    assert {key: env.get(key) for key in expected} == expected
