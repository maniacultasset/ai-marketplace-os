"""AC12, AC13: the four workflow files and the composite action (EXECUTION-PLAN.md:245-260;
CANON.md:21, :77; CANON-ADDENDUM.md §11; ADR-0013 §5)."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

PR_FILES = {
    "ci-api.yml": [
        "api-lint",
        "api-typecheck",
        "api-tests",
        "tenant-isolation",
        "lint-imports",
        "bootstrap",
    ],
    "contracts.yml": ["contracts", "schemas-drift"],
    "ci-meta.yml": ["commit-lint", "gitleaks"],
}
PUSH_FILE = "main-gitleaks.yml"
ALL_FILES = [*PR_FILES, PUSH_FILE]
TEN_CHECKS = {job for jobs in PR_FILES.values() for job in jobs}
TIMEOUTS = {"ci-api.yml": 15, "contracts.yml": 10, "ci-meta.yml": 5, PUSH_FILE: 5}
ENV_JOBS = {
    "api-lint": "mos_test",
    "api-typecheck": "mos_test",
    "api-tests": "mos_test",
    "tenant-isolation": "mos_iso",
    "contracts": "mos_test",
    "schemas-drift": "mos_test",
    "bootstrap": "marketplace_os",
}
CI_JWT = "ci_only_not_a_real_secret_at_least_32_chars"
SHA_PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}$")


@pytest.fixture
def workflows(repo_root: Path, load_yaml: Callable[[Path], Any]) -> dict[str, dict[str, Any]]:
    directory = repo_root / ".github/workflows"
    return {path.name: load_yaml(path) for path in sorted(directory.glob("*.y*ml"))}


@pytest.fixture
def action(repo_root: Path, load_yaml: Callable[[Path], Any]) -> dict[str, Any]:
    data = load_yaml(repo_root / ".github/actions/py-setup/action.yml")
    assert isinstance(data, dict)
    return data


def _jobs(workflows: dict[str, dict[str, Any]]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for name, workflow in workflows.items():
        for job_id, job in workflow["jobs"].items():
            yield name, job_id, job


def _all_steps(
    workflows: dict[str, dict[str, Any]], action: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    for _, _, job in _jobs(workflows):
        yield from job["steps"]
    yield from action["runs"]["steps"]


def _env_blocks(workflows: dict[str, dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for workflow in workflows.values():
        yield workflow.get("env") or {}
    for _, _, job in _jobs(workflows):
        yield job.get("env") or {}
        for step in job["steps"]:
            yield step.get("env") or {}


def _compose_postgres_image(repo_root: Path, load_yaml: Callable[[Path], Any]) -> str:
    image: str = load_yaml(repo_root / "docker-compose.yml")["services"]["postgres"]["image"]
    return image


def test_exactly_four_workflow_files(workflows: dict[str, dict[str, Any]]) -> None:
    assert sorted(workflows) == sorted(ALL_FILES)


def test_job_ids_are_the_ten_checks_per_file(workflows: dict[str, dict[str, Any]]) -> None:
    for name, jobs in PR_FILES.items():
        assert list(workflows[name]["jobs"]) == jobs, name
    assert list(workflows[PUSH_FILE]["jobs"]) == ["gitleaks-main"]
    assert len(TEN_CHECKS) == 10


def test_no_path_filters(workflows: dict[str, dict[str, Any]]) -> None:
    for name, workflow in workflows.items():
        for trigger in (workflow["on"] or {}).values():
            if isinstance(trigger, dict):
                assert not {"paths", "paths-ignore"} & set(trigger), name


def test_triggers_pull_request_to_main_only(workflows: dict[str, dict[str, Any]]) -> None:
    for name in PR_FILES:
        on = workflows[name]["on"]
        assert list(on) == ["pull_request"], name
        assert on["pull_request"]["branches"] == ["main"], name
        assert set(on["pull_request"]) <= {"branches", "types"}, name
    assert workflows["ci-meta.yml"]["on"]["pull_request"]["types"] == [
        "opened",
        "edited",
        "synchronize",
        "reopened",
    ]
    push = workflows[PUSH_FILE]["on"]
    assert push == {"push": {"branches": ["main"]}}


def test_no_pull_request_target(workflows: dict[str, dict[str, Any]], repo_root: Path) -> None:
    for name, workflow in workflows.items():
        assert "pull_request_target" not in workflow["on"], name
        assert "pull_request_target" not in (repo_root / ".github/workflows" / name).read_text()


def test_jobs_have_no_name_matrix_if_needs(workflows: dict[str, dict[str, Any]]) -> None:
    for name, job_id, job in _jobs(workflows):
        assert not {"name", "strategy", "if", "needs", "continue-on-error"} & set(job), (
            name,
            job_id,
        )
        for step in job["steps"]:
            assert "continue-on-error" not in step, (name, job_id)


def test_workflow_permissions_empty_jobs_read_only(workflows: dict[str, dict[str, Any]]) -> None:
    for name, workflow in workflows.items():
        assert workflow["permissions"] == {}, name
    for name, job_id, job in _jobs(workflows):
        assert job["permissions"] == {"contents": "read"}, (name, job_id)
        assert job["runs-on"] == "ubuntu-latest", (name, job_id)


def test_every_uses_sha_pinned_or_local(
    workflows: dict[str, dict[str, Any]], action: dict[str, Any]
) -> None:
    uses = [step["uses"] for step in _all_steps(workflows, action) if "uses" in step]
    assert uses
    for ref in uses:
        assert ref.startswith("./.github/actions/") or SHA_PINNED.fullmatch(ref), ref


def test_checkout_never_persists_credentials(
    workflows: dict[str, dict[str, Any]], action: dict[str, Any]
) -> None:
    checkouts = [
        step
        for step in _all_steps(workflows, action)
        if "actions/checkout@" in step.get("uses", "")
    ]
    assert len(checkouts) == len(list(_jobs(workflows)))
    for step in checkouts:
        assert step["with"]["persist-credentials"] is False


def test_no_expression_inside_any_run(
    workflows: dict[str, dict[str, Any]], action: dict[str, Any]
) -> None:
    for step in _all_steps(workflows, action):
        assert "${{" not in str(step.get("run", "")), step


def test_no_secrets_except_github_token(repo_root: Path) -> None:
    for path in [*(repo_root / ".github").rglob("*.yml"), *(repo_root / ".github").rglob("*.yaml")]:
        refs = re.findall(r"secrets\.([A-Za-z0-9_]+)", path.read_text(encoding="utf-8"))
        assert set(refs) <= {"GITHUB_TOKEN"}, path


@pytest.mark.parametrize(("job_id", "database"), sorted(ENV_JOBS.items()))
def test_ci_env_block_exactly_three_vars(
    workflows: dict[str, dict[str, Any]], job_id: str, database: str
) -> None:
    jobs = {jid: job for _, jid, job in _jobs(workflows)}
    env = jobs[job_id]["env"]
    assert set(env) == {"APP_ENV", "DATABASE_URL", "JWT_SECRET"}
    assert env["APP_ENV"] == "local"
    assert env["JWT_SECRET"] == CI_JWT
    url = urlsplit(env["DATABASE_URL"])
    assert url.scheme == "postgresql+psycopg"
    assert url.hostname == "localhost"
    assert url.port == 5432
    assert url.path == f"/{database}"


def test_no_forbidden_env_keys(workflows: dict[str, dict[str, Any]]) -> None:
    for env in _env_blocks(workflows):
        for key in env:
            assert key not in {"ENV", "REDIS_URL"}, key
            assert not key.startswith("ARQ_"), key


def test_uv_version_single_pin_and_checksum(action: dict[str, Any], repo_root: Path) -> None:
    with (repo_root / "pyproject.toml").open("rb") as fh:
        assert tomllib.load(fh)["tool"]["uv"]["required-version"] == "==0.12.19"
    dockerfile = (repo_root / "apps/api/Dockerfile").read_text(encoding="utf-8")
    assert re.findall(r"astral-sh/uv:([0-9.]+)@", dockerfile) == ["0.12.19"]
    setup = [s for s in action["runs"]["steps"] if "astral-sh/setup-uv@" in s.get("uses", "")]
    assert len(setup) == 1
    assert setup[0]["with"]["version"] == "0.12.19"
    assert re.fullmatch(r"[0-9a-f]{64}", setup[0]["with"]["checksum"])


def test_each_job_runs_its_make_target(workflows: dict[str, dict[str, Any]]) -> None:
    for name, job_id, job in _jobs(workflows):
        target = "gitleaks" if job_id == "gitleaks-main" else job_id
        assert job["steps"][-1] == {"run": f"make {target}"} or (
            set(job["steps"][-1]) == {"run", "env"} and job["steps"][-1]["run"] == f"make {target}"
        ), (name, job_id)


def test_postgres_services_pinned_with_job_db(
    workflows: dict[str, dict[str, Any]], repo_root: Path, load_yaml: Callable[[Path], Any]
) -> None:
    image = _compose_postgres_image(repo_root, load_yaml)
    jobs = {jid: job for _, jid, job in _jobs(workflows)}
    for job_id, database in (("api-tests", "mos_test"), ("tenant-isolation", "mos_iso")):
        service = jobs[job_id]["services"]["postgres"]
        assert service["image"] == image
        assert service["env"]["POSTGRES_DB"] == database
    assert [jid for jid, job in jobs.items() if "services" in job] == [
        "api-tests",
        "tenant-isolation",
    ]


def test_psql_bootstrap_precedes_pytest(workflows: dict[str, dict[str, Any]]) -> None:
    jobs = {jid: job for _, jid, job in _jobs(workflows)}
    for job_id, database in (("api-tests", "mos_test"), ("tenant-isolation", "mos_iso")):
        runs = [str(step.get("run", "")) for step in jobs[job_id]["steps"]]
        psql = [i for i, run in enumerate(runs) if run.lstrip().startswith("psql ")]
        assert len(psql) == 1, job_id
        assert f"@localhost:5432/{database}" in runs[psql[0]]
        assert "-v ON_ERROR_STOP=1 -f scripts/db/0000_bootstrap.sql" in runs[psql[0]]
        assert psql[0] < runs.index(f"make {job_id}")


def test_timeouts_and_concurrency_set(workflows: dict[str, dict[str, Any]]) -> None:
    for name, workflow in workflows.items():
        assert workflow["concurrency"]["cancel-in-progress"] is (name != PUSH_FILE), name
        for job_id, job in workflow["jobs"].items():
            assert job["timeout-minutes"] == TIMEOUTS[name], (name, job_id)


def test_main_scan_uses_push_range(workflows: dict[str, dict[str, Any]]) -> None:
    job = workflows[PUSH_FILE]["jobs"]["gitleaks-main"]
    checkout = job["steps"][0]
    assert checkout["with"] == {"persist-credentials": False, "fetch-depth": 0}
    assert job["steps"][-1]["env"] == {
        "BASE_SHA": "${{ github.event.before }}",
        "HEAD_SHA": "${{ github.event.after }}",
    }
