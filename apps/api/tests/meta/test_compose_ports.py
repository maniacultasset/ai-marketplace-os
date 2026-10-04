"""Host-port interpolation for docker-compose.yml (ADR-0011 amendment)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

PORT_KEYS = {
    "PG_HOST_PORT": 5432,
    "MINIO_API_HOST_PORT": 9000,
    "MINIO_CONSOLE_HOST_PORT": 9001,
}
_SCRUB = (
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_FILE",
    "PG_HOST_PORT",
    "MINIO_API_HOST_PORT",
    "MINIO_CONSOLE_HOST_PORT",
    "API_HOST_PORT",
    "TEST_DATABASE_URL",
    "ISO_DATABASE_URL",
    "DATABASE_URL",
    "ALEMBIC_DATABASE_URL",
    "S3_ENDPOINT_URL",
    "PORT",
    "CI",
    "MAKEFLAGS",
)
_INTERP = re.compile(r"\$\{([A-Za-z0-9_]+)(:-([^}]*))?\}")
_PUBLISHED = re.compile(r"services\.[^.]+\.ports\.\d+\.published\Z")
_OVERRIDE_DIFF = re.compile(
    r"(?:services\.[^.]+\.ports\.\d+\.published|name"
    r"|networks\.default\.name|volumes\.[^.]+\.name)\Z"
)


def port_interp_problems(raw: str) -> list[str]:
    problems: list[str] = []
    for key, default in PORT_KEYS.items():
        expected = f"${{{key}:-{default}}}"
        if raw.count(expected) != 1:
            problems.append(f"{expected} count {raw.count(expected)}")
        for match in re.finditer(rf"\$\{{{re.escape(key)}[^}}]*\}}", raw):
            if match.group(0) != expected:
                problems.append(match.group(0))
    return problems


def _interp_text(value: str, env: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in PORT_KEYS or match.group(2) is None:
            return match.group(0)
        raw = env.get(key, "")
        return match.group(3) if raw == "" else raw

    return _INTERP.sub(replace, value)


def interpolate(compose: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    def walk(node: Any) -> Any:
        if isinstance(node, str):
            return _interp_text(node, env)
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {str(key): walk(value) for key, value in node.items()}
        return node

    walked = walk(compose)
    assert isinstance(walked, dict)
    return walked


def diff_paths(left: object, right: object, prefix: str = "") -> list[str]:
    if type(left) is not type(right):
        return [prefix]
    if isinstance(left, dict) and isinstance(right, dict):
        paths: list[str] = []
        for key in sorted(set(left) | set(right), key=str):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in left or key not in right:
                paths.append(path)
            else:
                paths.extend(diff_paths(left[key], right[key], path))
        return paths
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [prefix]
        paths = []
        for index, (item, other) in enumerate(zip(left, right, strict=False)):
            paths.extend(diff_paths(item, other, f"{prefix}.{index}"))
        return paths
    if left != right:
        return [prefix]
    return []


def _child_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for key in _SCRUB:
        env.pop(key, None)
    env["S3_ACCESS_KEY"] = "portproof-access"
    env["S3_SECRET_KEY"] = "portproof-secret"
    if overrides:
        env.update(overrides)
    return env


def _normalised_project(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "", name.lower())


def docker_compose_config(repo_root: Path, env: dict[str, str]) -> dict[str, Any]:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed")
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
        empty_env = handle.name
    try:
        completed = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                "docker-compose.yml",
                "--project-directory",
                str(repo_root),
                "--env-file",
                empty_env,
                "config",
                "--format",
                "json",
            ],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        Path(empty_env).unlink(missing_ok=True)
    assert completed.returncode == 0, completed.stderr
    data: dict[str, Any] = json.loads(completed.stdout)
    return data


@pytest.fixture
def compose(repo_root: Path, load_yaml: Callable[[Path], Any]) -> dict[str, Any]:
    data = load_yaml(repo_root / "docker-compose.yml")
    assert isinstance(data, dict)
    return data


def _published(config: dict[str, Any], service: str) -> list[dict[str, Any]]:
    ports = config["services"][service]["ports"]
    assert isinstance(ports, list)
    return ports


def test_port_keys_use_colon_dash_with_todays_default(repo_root: Path) -> None:
    raw = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
    assert port_interp_problems(raw) == []
    seeded = "${PG_HOST_PORT}\n${PG_HOST_PORT:-5433}\n"
    flagged = port_interp_problems(seeded)
    assert "${PG_HOST_PORT}" in flagged
    assert "${PG_HOST_PORT:-5433}" in flagged


def test_defaults_resolve_to_todays_ports(compose: dict[str, Any]) -> None:
    resolved = interpolate(compose, {})
    postgres = resolved["services"]["postgres"]["ports"][0]
    minio = resolved["services"]["minio"]["ports"]
    assert postgres == {"target": 5432, "published": "5432", "host_ip": "127.0.0.1"}
    assert minio[0]["published"] == "9000"
    assert minio[0]["target"] == 9000
    assert minio[1]["published"] == "9001"
    assert minio[1]["target"] == 9001
    assert minio[0]["host_ip"] == "127.0.0.1"
    assert minio[1]["host_ip"] == "127.0.0.1"


def test_empty_values_mean_default(compose: dict[str, Any]) -> None:
    env = dict.fromkeys(PORT_KEYS, "")
    assert interpolate(compose, env) == interpolate(compose, {})


def test_overrides_change_only_published(compose: dict[str, Any]) -> None:
    base = interpolate(compose, {})
    over = interpolate(
        compose,
        {"PG_HOST_PORT": "5433", "MINIO_API_HOST_PORT": "9010", "MINIO_CONSOLE_HOST_PORT": "9011"},
    )
    paths = diff_paths(base, over)
    assert paths
    assert all(_PUBLISHED.fullmatch(path) for path in paths)
    seeded = {
        "services": {
            "postgres": {
                "ports": [
                    {
                        "target": "${PG_HOST_PORT:-5432}",
                        "published": "${PG_HOST_PORT:-5432}",
                        "host_ip": "127.0.0.1",
                    }
                ]
            }
        }
    }
    bad = diff_paths(interpolate(seeded, {}), interpolate(seeded, {"PG_HOST_PORT": "5433"}))
    assert any(path.endswith(".target") for path in bad)
    assert not all(_PUBLISHED.fullmatch(path) for path in bad)


def test_container_side_untouched_by_overrides(compose: dict[str, Any]) -> None:
    over = interpolate(
        compose,
        {"PG_HOST_PORT": "5433", "MINIO_API_HOST_PORT": "9010", "MINIO_CONSOLE_HOST_PORT": "9011"},
    )
    command = over["services"]["minio"]["command"]
    assert ":9001" in command
    host = over["services"]["minio-init"]["environment"]["MC_HOST_local"]
    assert "@minio:9000" in host


def test_docker_config_defaults(repo_root: Path) -> None:
    config = docker_compose_config(repo_root, _child_env())
    assert config["name"] == _normalised_project(repo_root.name)
    postgres = _published(config, "postgres")
    minio = _published(config, "minio")
    assert postgres[0]["published"] == "5432"
    assert postgres[0]["host_ip"] == "127.0.0.1"
    assert minio[0]["published"] == "9000"
    assert minio[1]["published"] == "9001"
    assert minio[0]["host_ip"] == "127.0.0.1"
    assert minio[1]["host_ip"] == "127.0.0.1"


def test_docker_config_overrides_and_project_name(repo_root: Path) -> None:
    base = docker_compose_config(repo_root, _child_env())
    over = docker_compose_config(
        repo_root,
        _child_env(
            {
                "PG_HOST_PORT": "5433",
                "MINIO_API_HOST_PORT": "9010",
                "MINIO_CONSOLE_HOST_PORT": "9011",
                "COMPOSE_PROJECT_NAME": "mkos-portproof",
            }
        ),
    )
    paths = diff_paths(base, over)
    assert paths
    assert all(_OVERRIDE_DIFF.fullmatch(path) for path in paths)
    seeded = diff_paths(
        {"services": {"postgres": {"ports": [{"target": 5432, "published": "5432"}]}}},
        {"services": {"postgres": {"ports": [{"target": 5433, "published": "5433"}]}}},
    )
    assert not all(_OVERRIDE_DIFF.fullmatch(path) for path in seeded)
