"""The oasdiff leg run for real against seeded removals (S3 round 2 re-check of R1-1).

Runs scripts/ci/oasdiff_check.sh in a throwaway git repo whose base commit holds the committed
openapi.json, then edits the working copy. Skips when docker or the pinned image is not available
locally; the test never pulls.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path("scripts/ci/oasdiff_check.sh")
_LEVELS = Path("scripts/ci/oasdiff-levels.txt")
_IGNORE = Path("scripts/ci/oasdiff-err-ignore.txt")
_SPEC = Path("packages/schemas/jsonschema/openapi.json")

Spec = dict[str, Any]


def _image(repo_root: Path) -> str:
    found: list[str] = re.findall(r'image="([^"]+)"', (repo_root / _SCRIPT).read_text("utf-8"))
    assert len(found) == 1
    return found[0]


def _require_image(repo_root: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed")
    probe = subprocess.run(
        ["docker", "image", "inspect", _image(repo_root)],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("pinned oasdiff image not present locally (no pulls in tests)")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo(repo_root: Path, tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    for rel in (_SCRIPT, _LEVELS, _IGNORE, _SPEC):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(repo_root / rel, repo / rel)
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-q",
        "-m",
        "base",
    )
    return repo


def _drop_optional_property(spec: Spec) -> None:
    del spec["components"]["schemas"]["ProductOut"]["properties"]["parent_product_id"]


def _drop_next_cursor(spec: Spec) -> None:
    del spec["components"]["schemas"]["PageInfo"]["properties"]["next_cursor"]


def _drop_error_details(spec: Spec) -> None:
    del spec["components"]["schemas"]["ErrorBody"]["properties"]["details"]


def _drop_error_status(spec: Spec) -> None:
    del spec["paths"]["/api/v1/images/upload"]["post"]["responses"]["404"]


def _drop_optional_header(spec: Spec) -> None:
    callback = spec["paths"]["/api/v1/marketplaces/{marketplace}/callback"]["get"]
    del callback["responses"]["303"]["headers"]["Location"]


@pytest.mark.parametrize(
    ("mutate", "rule"),
    [
        (_drop_optional_property, "response-optional-property-removed"),
        (_drop_next_cursor, "response-optional-property-removed"),
        (_drop_error_details, "response-optional-property-removed"),
        (_drop_error_status, "response-non-success-status-removed"),
        (_drop_optional_header, "optional-response-header-removed"),
    ],
)
def test_oasdiff_reds_on_info_rated_removal(
    repo_root: Path, tmp_path: Path, mutate: Callable[[Spec], None], rule: str
) -> None:
    _require_image(repo_root)
    repo = _repo(repo_root, tmp_path)
    spec = json.loads((repo / _SPEC).read_text("utf-8"))
    head = copy.deepcopy(spec)
    mutate(head)
    (repo / _SPEC).write_text(json.dumps(head, indent=2) + "\n", encoding="utf-8")
    out = subprocess.run(
        ["bash", str(_SCRIPT), "HEAD"], cwd=repo, capture_output=True, text=True, check=False
    )
    assert out.returncode != 0, out.stdout
    assert f"[{rule}]" in out.stdout + out.stderr


def test_oasdiff_green_on_unchanged_spec(repo_root: Path, tmp_path: Path) -> None:
    _require_image(repo_root)
    repo = _repo(repo_root, tmp_path)
    out = subprocess.run(
        ["bash", str(_SCRIPT), "HEAD"], cwd=repo, capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "oasdiff: no breaking change against HEAD" in out.stdout
