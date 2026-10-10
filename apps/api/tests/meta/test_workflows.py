"""AC12, AC13: the workflow files and the composite action (EXECUTION-PLAN.md:245-260;
CANON.md:21, :77; CANON-ADDENDUM.md §11; ADR-0013 §5). S26 split api-tests into two required
jobs (api-tests, api-tests-integration; KNOWN-ISSUES §E), so there are 11 required checks."""

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
        "api-tests-integration",
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
REQUIRED_CHECKS = {job for jobs in PR_FILES.values() for job in jobs}
# The api suite's two shards (tests/support/shard.py): the same services, steps and env.
API_TEST_JOBS = ("api-tests", "api-tests-integration")
TIMEOUTS = {"ci-api.yml": 15, "contracts.yml": 10, "ci-meta.yml": 5, PUSH_FILE: 5, PUBLISH_FILE: 10}
# Per-job exceptions: the api suite with coverage outgrew 15 min on the runner (S13b); since S26
# each of its two shards stays under ~25 min, capped at 30.
JOB_TIMEOUTS = {("ci-api.yml", "api-tests"): 30, ("ci-api.yml", "api-tests-integration"): 30}
ENV_JOBS = {
    "api-lint": "mos_test",
    "api-typecheck": "mos_test",
    "api-tests": "mos_test",
    "api-tests-integration": "mos_test",
    "tenant-isolation": "mos_iso",
    "contracts": "mos_test",
    "schemas-drift": "mos_test",
    "bootstrap": "marketplace_os",
}
CI_JWT = "ci_only_not_a_real_secret_at_least_32_chars"
SHA_PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}$")


NIGHTLY_FILE = "nightly.yml"
NIGHTLY_LEGS = [
    "pip-audit",
    "gitleaks-history",
    "mutation",
    "fuzz",
    "hypothesis-deep",
    "random-order",
    "migration-replay",
    "acceptance",
    "drill",
]
NIGHTLY_LIGHT = {"pip-audit", "gitleaks-history"}


@pytest.fixture
def workflows(repo_root: Path, load_yaml: Callable[[Path], Any]) -> dict[str, dict[str, Any]]:
    """The PR, push and tag workflows (S26 C1: the nightly has its own fixture and tests)."""
    directory = repo_root / ".github/workflows"
    return {name: load_yaml(directory / name) for name in sorted(ALL_FILES)}


@pytest.fixture
def all_workflows(repo_root: Path, load_yaml: Callable[[Path], Any]) -> dict[str, dict[str, Any]]:
    directory = repo_root / ".github/workflows"
    return {path.name: load_yaml(path) for path in sorted(directory.glob("*.y*ml"))}


@pytest.fixture
def nightly(repo_root: Path, load_yaml: Callable[[Path], Any]) -> dict[str, Any]:
    data = load_yaml(repo_root / ".github/workflows" / NIGHTLY_FILE)
    assert isinstance(data, dict)
    return data


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


def test_exactly_five_workflow_files(all_workflows: dict[str, dict[str, Any]]) -> None:
    """The five PR/push/tag files plus the S26 nightly (C1)."""
    assert sorted(all_workflows) == sorted([*ALL_FILES, NIGHTLY_FILE])


def test_job_ids_are_the_required_checks_per_file(workflows: dict[str, dict[str, Any]]) -> None:
    for name, jobs in PR_FILES.items():
        assert list(workflows[name]["jobs"]) == jobs, name
    assert list(workflows[PUSH_FILE]["jobs"]) == ["gitleaks-main"]
    assert len(REQUIRED_CHECKS) == 11


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


def test_no_pull_request_target(all_workflows: dict[str, dict[str, Any]], repo_root: Path) -> None:
    for name, workflow in all_workflows.items():
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
    all_workflows: dict[str, dict[str, Any]], action: dict[str, Any]
) -> None:
    uses = [step["uses"] for step in _all_steps(all_workflows, action) if "uses" in step]
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
    pairs = [*((job, "mos_test") for job in API_TEST_JOBS), ("tenant-isolation", "mos_iso")]
    for job_id, database in pairs:
        service = jobs[job_id]["services"]["postgres"]
        assert service["image"] == image
        assert service["env"]["POSTGRES_DB"] == database
    assert [jid for jid, job in jobs.items() if "services" in job] == [
        *API_TEST_JOBS,
        "tenant-isolation",
    ]


def test_psql_bootstrap_precedes_pytest(workflows: dict[str, dict[str, Any]]) -> None:
    jobs = {jid: job for _, jid, job in _jobs(workflows)}
    pairs = [*((job, "mos_test") for job in API_TEST_JOBS), ("tenant-isolation", "mos_iso")]
    for job_id, database in pairs:
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


@pytest.mark.parametrize("job_id", ["tenant-isolation", *API_TEST_JOBS])
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
    for api_job in API_TEST_JOBS:
        runs = [str(step.get("run", "")) for step in jobs[api_job]["steps"]]
        assert runs[-1].strip() == f"make {api_job}"
        up = next(i for i, run in enumerate(runs) if "docker compose up -d --wait minio" in run)
        init = next(i for i, run in enumerate(runs) if "docker compose run --rm minio-init" in run)
        assert up <= init < len(runs) - 1
        assert runs[up].index("up -d --wait minio") < runs[init].index("run --rm minio-init")
    for _, job_id, job in _jobs(workflows):
        if job_id in (*API_TEST_JOBS, "bootstrap"):
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


def test_api_test_jobs_share_one_shape(workflows: dict[str, dict[str, Any]]) -> None:
    """The two shards differ only in their make target (and the explanatory comment)."""
    first, second = (workflows["ci-api.yml"]["jobs"][job] for job in API_TEST_JOBS)
    assert first["services"] == second["services"]
    assert first["env"] == second["env"]
    assert first["steps"][:-1] == second["steps"][:-1]
    assert [first["steps"][-1], second["steps"][-1]] == [
        {"run": f"make {job}"} for job in API_TEST_JOBS
    ]


def test_required_check_lists_name_every_check(repo_root: Path) -> None:
    """The PR template and the README's check table list every required check."""
    template = (repo_root / ".github/pull_request_template.md").read_text(encoding="utf-8")
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    for check in REQUIRED_CHECKS:
        assert f"| `{check}` |" in template, check
        assert f"| `{check}` |" in readme, check
    assert f"all {len(REQUIRED_CHECKS)} green" in template


# ---- S26 T24 (AC 12; PLAN D8; C1): the nightly workflow, never a required check ----


def _on(workflow: dict[Any, Any]) -> Any:
    """PyYAML reads the bare key `on` as the boolean True."""
    return workflow["on"] if "on" in workflow else workflow[True]


def test_nightly_triggers_schedule_and_dispatch_only(nightly: dict[str, Any]) -> None:
    on = _on(nightly)
    assert set(on) == {"schedule", "workflow_dispatch"}
    assert on["schedule"] == [{"cron": "30 20 * * *"}]  # 02:00 IST
    force = on["workflow_dispatch"]["inputs"]["force"]
    assert force["type"] == "boolean" and force["default"] is False
    assert list(nightly["jobs"]) == [*NIGHTLY_LEGS, "report"]
    assert nightly["concurrency"] == {"group": "nightly", "cancel-in-progress": False}


def test_nightly_permissions_empty_top_level(nightly: dict[str, Any]) -> None:
    assert nightly["permissions"] == {}
    for leg in NIGHTLY_LEGS:
        assert nightly["jobs"][leg]["permissions"] == {"contents": "read"}, leg


def test_nightly_only_report_job_has_issues_write(nightly: dict[str, Any]) -> None:
    writers = {
        job_id
        for job_id, job in nightly["jobs"].items()
        if any(v == "write" for v in job["permissions"].values())
    }
    assert writers == {"report"}
    assert nightly["jobs"]["report"]["permissions"] == {"issues": "write"}


def test_nightly_report_needs_all_if_failure(nightly: dict[str, Any]) -> None:
    report = nightly["jobs"]["report"]
    assert report["needs"] == NIGHTLY_LEGS and report["if"] == "failure()"
    (step,) = report["steps"]
    assert "uses" not in step  # no checkout, no action: the gh CLI on the runner
    assert step["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    assert step["env"]["NEEDS"] == "${{ toJSON(needs) }}"
    run = step["run"]
    assert "gh issue create --title nightly-red --label nightly-red" in run
    assert "gh issue comment" in run and "--label nightly-red" in run
    assert 'body="Nightly run $RUN_URL failed: $failed"' in run  # URL and leg names only
    for leg in NIGHTLY_LEGS:
        assert "if" not in nightly["jobs"][leg] and "needs" not in nightly["jobs"][leg], leg


def test_nightly_actions_sha_pinned(nightly: dict[str, Any]) -> None:
    for leg in NIGHTLY_LEGS:
        steps = nightly["jobs"][leg]["steps"]
        assert SHA_PINNED.fullmatch(steps[0]["uses"].split(" ")[0]), leg
        assert steps[0]["uses"].startswith("actions/checkout@"), leg
        assert steps[0]["with"]["persist-credentials"] is False, leg
        assert steps[1] == {"uses": "./.github/actions/py-setup"}, leg
        assert steps[2]["run"] == f"scripts/ci/nightly.sh {leg}", leg
        assert steps[2]["env"] == {"NIGHTLY_FORCE": "${{ inputs.force }}"}, leg
        assert nightly["jobs"][leg]["runs-on"] == "ubuntu-latest", leg
        assert 5 <= nightly["jobs"][leg]["timeout-minutes"] <= 360, leg
    assert nightly["jobs"]["gitleaks-history"]["steps"][0]["with"]["fetch-depth"] == 0


def test_nightly_no_secrets_but_github_token(repo_root: Path) -> None:
    text = (repo_root / ".github/workflows" / NIGHTLY_FILE).read_text(encoding="utf-8")
    assert set(re.findall(r"secrets\.([A-Za-z0-9_]+)", text)) == {"GITHUB_TOKEN"}
    assert text.count("secrets.GITHUB_TOKEN") == 1
    assert "pull_request" not in text


def test_nightly_no_expression_in_run(nightly: dict[str, Any]) -> None:
    for job_id, job in nightly["jobs"].items():
        for step in job["steps"]:
            assert "${{" not in str(step.get("run", "")), job_id


def test_nightly_heavy_legs_use_skip_guard(repo_root: Path) -> None:
    script = (repo_root / "scripts/ci/nightly.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in script
    light = re.search(r'^LIGHT="([^"]+)"', script, re.MULTILINE)
    heavy = re.search(r'^HEAVY="([^"]+)"', script, re.MULTILINE)
    assert light is not None and heavy is not None
    assert set(light.group(1).split()) == NIGHTLY_LIGHT
    assert set(heavy.group(1).split()) == set(NIGHTLY_LEGS) - NIGHTLY_LIGHT
    guard = script.index(
        'python3 scripts/ci/nightly_skip.py "$last" --force "${NIGHTLY_FORCE:-false}"'
    )
    assert guard < script.index('case "$leg" in\n  pip-audit)')
    assert "git log -1 --format=%ct origin/main" in script
    for leg in NIGHTLY_LEGS:
        assert f"\n  {leg})\n" in script, leg
    assert "make fuzz FUZZ_MAX_EXAMPLES=500" in script
    assert "HYPOTHESIS_PROFILE=nightly" in script
    assert "for seed in 1 2 3; do" in script and "--randomly-seed=$seed" in script
    assert 'for step in "upgrade head" "downgrade base" "upgrade head"; do' in script
    for target in ("make mutation", "make acceptance", "make drill-restore"):
        assert target in script, target


def test_nightly_no_cloud_auth_action(repo_root: Path, nightly: dict[str, Any]) -> None:
    text = (repo_root / ".github/workflows" / NIGHTLY_FILE).read_text(encoding="utf-8")
    script = (repo_root / "scripts/ci/nightly.sh").read_text(encoding="utf-8")
    for banned in (
        "google-github-actions",
        "aws-actions",
        "azure/login",
        "id-token",
        "gcloud",
        "tofu",
        "docker push",
        "deploy",
    ):
        assert banned not in text, banned
        assert banned not in script, banned
    uses = {
        step["uses"].split("@")[0]
        for job in nightly["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    }
    assert uses == {"actions/checkout", "./.github/actions/py-setup"}


def test_nightly_is_not_a_required_check(repo_root: Path) -> None:
    template = (repo_root / ".github/pull_request_template.md").read_text(encoding="utf-8")
    for leg in NIGHTLY_LEGS:
        assert f"| `{leg}` |" not in template, leg
