"""AC12, AC13: the four workflow files and the composite action (EXECUTION-PLAN.md:245-260;
CANON.md:21, :77; CANON-ADDENDUM.md §11; ADR-0013 §5)."""

from __future__ import annotations

import json
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
PUBLISH_FILE = "publish-schemas.yml"
ALL_FILES = [*PR_FILES, PUSH_FILE, PUBLISH_FILE]
TEN_CHECKS = {job for jobs in PR_FILES.values() for job in jobs}
TIMEOUTS = {"ci-api.yml": 15, "contracts.yml": 10, "ci-meta.yml": 5, PUSH_FILE: 5, PUBLISH_FILE: 10}
# Per-job exceptions: the full api suite with coverage outgrew 15 min on the runner (S13b).
JOB_TIMEOUTS = {("ci-api.yml", "api-tests"): 45}
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


def test_exactly_five_workflow_files(workflows: dict[str, dict[str, Any]]) -> None:
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
        if name == PUBLISH_FILE:
            # The one job that writes: publishing the npm package to GitHub Packages.
            assert job["permissions"] == {"contents": "read", "packages": "write"}, job_id
        else:
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
        # Never cancel a main scan or a release mid-way.
        assert workflow["concurrency"]["cancel-in-progress"] is (
            name not in {PUSH_FILE, PUBLISH_FILE}
        ), name
        for job_id, job in workflow["jobs"].items():
            want = JOB_TIMEOUTS.get((name, job_id), TIMEOUTS[name])
            assert job["timeout-minutes"] == want, (name, job_id)


def test_main_scan_uses_push_range(workflows: dict[str, dict[str, Any]]) -> None:
    job = workflows[PUSH_FILE]["jobs"]["gitleaks-main"]
    checkout = job["steps"][0]
    assert checkout["with"] == {"persist-credentials": False, "fetch-depth": 0}
    assert job["steps"][-1]["env"] == {
        "BASE_SHA": "${{ github.event.before }}",
        "HEAD_SHA": "${{ github.event.after }}",
    }


def test_schemas_drift_sets_up_node_pinned(
    workflows: dict[str, dict[str, Any]], repo_root: Path
) -> None:
    # S3 T18: the drift job builds the TS types, so it needs Node 24 and the pinned pnpm.
    steps = workflows["contracts.yml"]["jobs"]["schemas-drift"]["steps"]
    node = [s for s in steps if "actions/setup-node@" in s.get("uses", "")]
    assert len(node) == 1
    assert SHA_PINNED.fullmatch(node[0]["uses"])
    assert node[0]["with"] == {"node-version": "24", "package-manager-cache": False}
    runs = [s.get("run") for s in steps]
    assert runs.index("corepack enable pnpm") < runs.index("make schemas-drift")
    assert steps.index(node[0]) < runs.index("corepack enable pnpm")
    package = json.loads((repo_root / "packages/schemas/ts/package.json").read_text("utf-8"))
    assert re.fullmatch(r"pnpm@\d+\.\d+\.\d+\+sha512\.[0-9a-f]{128}", package["packageManager"])
    contracts_steps = workflows["contracts.yml"]["jobs"]["contracts"]["steps"]
    assert not [s for s in contracts_steps if "setup-node" in s.get("uses", "")]


def test_contracts_job_runs_mock_smoke(workflows: dict[str, dict[str, Any]]) -> None:
    # S3 T21: the mock answers on a real socket before the contracts check runs.
    steps = workflows["contracts.yml"]["jobs"]["contracts"]["steps"]
    runs = [s.get("run") for s in steps]
    assert "make mock-smoke" in runs
    assert runs.index("make mock-smoke") < len(steps) - 1
    assert steps[-1] == {"run": "make contracts"}
    assert steps[0]["with"]["fetch-depth"] == 0


def test_publish_tag_trigger_only(workflows: dict[str, dict[str, Any]]) -> None:
    assert workflows[PUBLISH_FILE]["on"] == {"push": {"tags": ["schemas-v*"]}}
    assert list(workflows[PUBLISH_FILE]["jobs"]) == ["publish-schemas"]


def test_publish_no_pull_request_target(
    workflows: dict[str, dict[str, Any]], repo_root: Path
) -> None:
    on = workflows[PUBLISH_FILE]["on"]
    assert set(on) == {"push"}
    text = (repo_root / ".github/workflows" / PUBLISH_FILE).read_text(encoding="utf-8")
    assert "pull_request" not in text
    assert "workflow_dispatch" not in text


def test_publish_token_only_in_env(workflows: dict[str, dict[str, Any]], repo_root: Path) -> None:
    job = workflows[PUBLISH_FILE]["jobs"]["publish-schemas"]
    last = job["steps"][-1]
    assert last == {
        "run": "make publish-schemas",
        "env": {"NODE_AUTH_TOKEN": "${{ secrets.GITHUB_TOKEN }}"},
    }
    text = (repo_root / ".github/workflows" / PUBLISH_FILE).read_text(encoding="utf-8")
    assert text.count("secrets.GITHUB_TOKEN") == 1
    for step in job["steps"][:-1]:
        assert "NODE_AUTH_TOKEN" not in str(step)
    checkout = job["steps"][0]
    assert checkout["with"] == {"persist-credentials": False, "fetch-depth": 0}


@pytest.mark.parametrize("job_id", ["tenant-isolation", "api-tests"])
def test_db_jobs_set_app_rw_password(workflows: dict[str, dict[str, Any]], job_id: str) -> None:
    """S9 D14: the job sets app_rw's CI-only password from the Makefile constant, as a verifier.
    api-tests needs it too since S10b: its RLS legs log in as app_rw (tests/support/dsn.py)."""
    jobs = {jid: job for _, jid, job in _jobs(workflows)}
    runs = [str(step.get("run", "")) for step in jobs[job_id]["steps"]]
    bootstrap = next(run for run in runs if run.lstrip().startswith("psql "))
    assert 'APP_RW_SCRAM="$(make -s app-rw-scram)" psql' in bootstrap
    assert "-f scripts/db/0000_role_passwords.sql" in bootstrap
    assert bootstrap.index("0000_bootstrap.sql") < bootstrap.index("0000_role_passwords.sql")
    assert "ci_only_app_rw" not in bootstrap


# ---- S17b T0a (CP10): the image suites' MinIO in the required api-tests job ----


def test_api_tests_starts_compose_minio_before_pytest(workflows: dict[str, dict[str, Any]]) -> None:
    """The compose MinIO and its buckets start before `make api-tests` (still the last step), so
    an `M` suite (image upload flow, signed PUTs, the orphan sweep) runs in the required check.
    No other job runs `docker compose`."""
    jobs = {jid: job for _, jid, job in _jobs(workflows)}
    runs = [str(step.get("run", "")) for step in jobs["api-tests"]["steps"]]
    assert runs[-1].strip() == "make api-tests"
    up = next(i for i, run in enumerate(runs) if "docker compose up -d --wait minio" in run)
    init = next(i for i, run in enumerate(runs) if "docker compose run --rm minio-init" in run)
    assert up <= init < len(runs) - 1
    assert runs[up].index("up -d --wait minio") < runs[init].index("run --rm minio-init")
    for _, job_id, job in _jobs(workflows):
        if job_id in ("api-tests", "bootstrap"):
            continue
        assert not any("docker compose" in str(s.get("run", "")) for s in job["steps"]), job_id


def test_compose_minio_init_creates_images_bucket(
    repo_root: Path, load_yaml: Callable[[Path], Any]
) -> None:
    command = load_yaml(repo_root / "docker-compose.yml")["services"]["minio-init"]["command"]
    assert "local/marketplace-images" in command
    assert "local/marketplace-csv" in command


def test_config_storage_defaults_match_env_example(repo_root: Path) -> None:
    """The job needs no S3 env beyond the dev credentials: the settings' storage defaults are the
    `.env.example` values (bucket and keys)."""
    from app.core.config import Settings

    lines = (repo_root / ".env.example").read_text(encoding="utf-8").splitlines()
    example = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))
    fields = Settings.model_fields
    for name in ("GCS_BUCKET_IMAGES", "S3_ENDPOINT_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY"):
        assert fields[name.lower()].default == example[name], name
