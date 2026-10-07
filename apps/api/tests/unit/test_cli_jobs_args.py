"""S13 A15: the staff `jobs` commands refuse bad arguments with argparse's exit 2, before any
connection opens."""

from __future__ import annotations

import uuid

import pytest
from app import cli

JOB = str(uuid.uuid4())


@pytest.mark.parametrize(
    "argv",
    [
        ["jobs", "requeue", JOB],
        ["jobs", "requeue", JOB, "--staff-email", "ops@x.test"],
        ["jobs", "requeue", JOB, "--reason", "fixed upstream"],
        ["jobs", "requeue", JOB, "--staff-email", "not-an-email", "--reason", "r"],
        ["jobs", "requeue", JOB, "--staff-email", "ops@x.test", "--reason", "   "],
        ["jobs", "requeue", "not-a-uuid", "--staff-email", "ops@x.test", "--reason", "r"],
        ["jobs", "dead-letter", "list", "--limit", "0"],
        ["jobs", "dead-letter", "list", "--limit", "501"],
        ["jobs", "dead-letter", "list", "--limit", "ten"],
        ["jobs", "dead-letter"],
        ["jobs"],
    ],
)
def test_cli_jobs_requires_email_and_reason(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_db(*args: object, **kwargs: object) -> None:
        raise AssertionError("no command may run on bad arguments")

    monkeypatch.setattr(cli, "run_requeue", no_db)
    monkeypatch.setattr(cli, "run_dead_letter_list", no_db)
    with pytest.raises(SystemExit) as caught:
        cli.main(argv)
    assert caught.value.code == 2
