"""S16 tester (finish run, AC10): every check in `scripts/e2e/brand_onboarding.sh` really goes
red. The happy-path stubs of `test_scripts.py` are bent one behaviour at a time; the script must
then exit 1 at exactly that step, print no response body, and still run WORKER_STOP when the
worker was started."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from tests.meta.test_scripts import (
    _BRAND_CLI_STUB,
    _BRAND_CURL_STUB,
    _BRAND_DOC,
    _BRAND_DOCKER_STUB,
    _BRAND_TENANT,
    BRAND_FLOW,
)

# step -> (stub name, original text, replacement): one broken behaviour each
BREAKS: dict[str, tuple[str, str, str]] = {
    "cli_duplicate_slug_fails": ("cli", "sys.exit(4)", "pass"),
    "get_tenant_me_etag": ("curl", """'"v1"'\nelif""", "None\nelif"),
    "patch_tenant_me_200": (
        "curl",
        """status, body, etag = 200, patch, '"v2"'""",
        """status, body, etag = 200, patch, '"v1"'""",
    ),
    "patch_stale_409": ("curl", 'and not flag("patched")', "or True"),
    "upload_202": (
        "curl",
        'status, body = 202, {"job_id": "JOB"}',
        'status, body = 500, {"error": {"type": "INTERNAL_ERROR"}}',
    ),
    "job_succeeded_under_30s": ("curl", '{"status": "succeeded"}', '{"status": "failed"}'),
    "list_ready_with_chunks": (
        "curl",
        '"status": "ready", "chunk_count": 5',
        '"status": "ready", "chunk_count": 0',
    ),
    "reupload_new_key_same_job": (
        "curl",
        'status, body = 202, {"job_id": "JOB"}',
        'status, body = 202, {"job_id": "JOB2" if "again" in key else "JOB"}',
    ),
    "key_reuse_other_file_409": ("curl", '"other.md" in form', '"other.md" in form and False'),
    "cli_add_member_reviewer": ("cli", 'print("tenant_id=%s user_id=', 'sys.exit(3)\n    print("x'),
    "reviewer_upload_403": ("curl", 'token.startswith("reviewer-")', "False"),
    "bharat_list_lacks": ("curl", 'rows = [] if "bharat" in token else [', "rows = ["),
    "bharat_delete_404": (
        "curl",
        'token.startswith("owner-") and not flag("deleted")',
        'not flag("deleted")',
    ),
    "delete_204": ("curl", 'token.startswith("owner-") and not flag("deleted")', "False"),
    "delete_again_404": (
        "curl",
        'token.startswith("owner-") and not flag("deleted")',
        'token.startswith("owner-")',
    ),
    "db_doc_chunks_inactive": ("docker", 'else "0 5"', 'else "1 5"'),
}
WORKER_STEPS_FROM = "upload_202"  # the worker is up from worker_start on


def _run(
    repo_root: Path, tmp_path: Path, step: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    stubs = {"curl": _BRAND_CURL_STUB, "docker": _BRAND_DOCKER_STUB, "cli": _BRAND_CLI_STUB}
    which, old, new = BREAKS[step]
    assert stubs[which].count(old) == 1, (step, old)  # the bend still matches the stub
    stubs[which] = stubs[which].replace(old, new)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    for name, text in (
        ("curl", stubs["curl"]),
        ("docker", stubs["docker"]),
        ("mkos-cli", stubs["cli"]),
    ):
        path = bin_dir / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
    hook_log = stub_dir / "hooks.log"
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env |= {
        "STUB_DIR": str(stub_dir),
        "STUB_TENANT": _BRAND_TENANT,
        "STUB_DOC": _BRAND_DOC,
        "WORKER_START": f'echo start >> "{hook_log}"',
        "WORKER_STOP": f'echo stop >> "{hook_log}"',
        "MKOS_CLI": "mkos-cli",
    }
    result = subprocess.run(
        [str(repo_root / BRAND_FLOW), "http://127.0.0.1:8123"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    return result, hook_log


@pytest.mark.parametrize("step", sorted(BREAKS))
def test_brand_flow_goes_red_at_the_broken_step(repo_root: Path, tmp_path: Path, step: str) -> None:
    result, hook_log = _run(repo_root, tmp_path, step)
    assert result.returncode == 1, (step, result.returncode, result.stderr)
    assert f"e2e: FAILED at step {step} (HTTP status" in result.stderr, result.stderr
    assert "brand_onboarding OK" not in result.stdout
    # never a response body (the stubs' bodies are JSON objects)
    assert "{" not in result.stderr and '"type"' not in result.stderr
    hooks = hook_log.read_text(encoding="utf-8").splitlines() if hook_log.exists() else []
    steps = [
        line.split()[-1] for line in result.stdout.splitlines() if line.startswith("e2e: step ")
    ]
    if WORKER_STEPS_FROM in steps:
        assert hooks.count("start") == 1 and hooks[-1] == "stop", hooks  # the trap stops it
    else:
        assert "start" not in hooks and "stop" not in hooks, hooks
