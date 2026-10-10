"""AC9 and the MinIO services (TEAM-QUESTIONS T107): docker-compose.yml (CANON-ADDENDUM.md §1
postgres, pinned by digest, loopback ports).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

DIGEST_IMAGE = re.compile(r"^pgvector/pgvector:0\.8\.5-pg17@sha256:[0-9a-f]{64}$")
MINIO_IMAGE = re.compile(r"^cgr\.dev/chainguard/minio:latest@sha256:[0-9a-f]{64}$")
MINIO_CLIENT_IMAGE = re.compile(r"^cgr\.dev/chainguard/minio-client:latest@sha256:[0-9a-f]{64}$")
_ON_FAILURE_N = re.compile(r"^on-failure:[1-9][0-9]*$")
_BUCKET_TOKEN = re.compile(r"marketplace-[a-z]+")
_ACCESS_INTERP = "${S3_ACCESS_KEY:?run cp .env.example .env}"
_SECRET_INTERP = "${S3_SECRET_KEY:?run cp .env.example .env}"
_PORT_KEYS = {"target", "published", "host_ip"}
PORT_ENTRY = {
    "postgres": {
        "target": 5432,
        "published": "${PG_HOST_PORT:-5432}",
        "host_ip": "127.0.0.1",
    },
}


def canon_local_bucket_names(repo_root: Path) -> list[str]:
    for raw in (repo_root / "docs" / "canon" / "CANON.md").read_text(encoding="utf-8").splitlines():
        if raw.startswith("| Local bucket names |"):
            cells = raw.split("|")
            return _BUCKET_TOKEN.findall(cells[2])
    raise AssertionError("CANON.md Local bucket names row missing")


def _as_command(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(part) for part in value]
    return shlex.split(str(value))


def _needs_healthcheck(service: dict[str, Any]) -> bool:
    restart = str(service.get("restart", ""))
    return restart not in {"no", "on-failure"} and _ON_FAILURE_N.fullmatch(restart) is None


@pytest.fixture
def compose(repo_root: Path, load_yaml: Callable[[Path], Any]) -> dict[str, Any]:
    data = load_yaml(repo_root / "docker-compose.yml")
    assert isinstance(data, dict)
    return data


def _addendum_postgres(repo_root: Path) -> dict[str, Any]:
    addendum = (repo_root / "docs/canon/CANON-ADDENDUM.md").read_text(encoding="utf-8")
    section = addendum[addendum.index("## 1. Bootstrap SQL") : addendum.index("## 2. Bootstrap")]
    block = re.search(r"```yaml\n(.*?)```", section, flags=re.DOTALL)
    assert block is not None
    service: dict[str, Any] = yaml.safe_load(block.group(1))["postgres"]
    return service


def published_host_ip(entry: str | dict[str, Any]) -> str | None:
    """The host IP a `ports` entry binds, or None when it binds every interface."""
    if isinstance(entry, dict):
        host_ip = entry.get("host_ip")
        return str(host_ip) if host_ip else None
    spec = str(entry).split("/", 1)[0]
    parts = spec.rsplit(":", 2)
    return parts[0] if len(parts) == 3 and parts[0] else None


def _all_ports(compose: dict[str, Any]) -> list[str | dict[str, Any]]:
    return [p for service in compose["services"].values() for p in service.get("ports", [])]


def long_form_problems(entry: object) -> list[str]:
    """A published port is a dict whose keys are exactly target, published and host_ip."""
    if not isinstance(entry, dict):
        return ["not a mapping"]
    problems: list[str] = []
    keys = set(entry)
    if keys != _PORT_KEYS:
        problems.append(f"keys {sorted(keys)}")
    target = entry.get("target")
    if not isinstance(target, int):
        problems.append("target is not int")
    return problems


def test_no_version_key(compose: dict[str, Any]) -> None:
    assert "version" not in compose


def test_postgres_matches_addendum_with_pg17_image_and_loopback_port(
    compose: dict[str, Any], repo_root: Path
) -> None:
    actual = dict(compose["services"]["postgres"])
    expected = _addendum_postgres(repo_root)
    assert DIGEST_IMAGE.fullmatch(actual.pop("image"))
    assert actual.pop("ports") == [PORT_ENTRY["postgres"]]
    expected.pop("image")
    expected.pop("ports")
    assert actual == expected


def test_long_running_services_have_healthchecks(compose: dict[str, Any]) -> None:
    for name, service in compose["services"].items():
        if _needs_healthcheck(service):
            assert "healthcheck" in service, name


def test_named_volumes(compose: dict[str, Any]) -> None:
    declared = set(compose.get("volumes") or {})
    assert "pgdata" in declared
    assert "miniodata" in declared
    for name, service in compose["services"].items():
        for volume in service.get("volumes", []):
            source = str(volume).split(":", 1)[0]
            if not source.startswith((".", "/")):
                assert source in declared, (name, source)


def test_no_redis(compose: dict[str, Any]) -> None:
    services = compose["services"]
    assert not [name for name in services if "redis" in name.lower()]
    assert not [s for s in services.values() if "redis" in str(s.get("image", "")).lower()]


def test_published_ports_loopback_only(compose: dict[str, Any]) -> None:
    ports = _all_ports(compose)
    assert ports
    assert all(published_host_ip(entry) == "127.0.0.1" for entry in ports), ports
    # The check itself: a bare mapping, an all-interfaces IP and a long form with no host_ip.
    assert published_host_ip("5432:5432") is None
    assert published_host_ip("5432") is None
    assert published_host_ip("0.0.0.0:5432:5432") == "0.0.0.0"  # noqa: S104 - test input
    assert published_host_ip({"target": 5432, "published": 5432}) is None
    assert published_host_ip({"target": 5432, "published": "${PG_HOST_PORT:-5432}"}) is None
    off_loopback = {"target": 5432, "published": "5432", "host_ip": "0.0.0.0"}  # noqa: S104
    assert published_host_ip(off_loopback) == off_loopback["host_ip"]


def test_every_port_entry_is_long_form(compose: dict[str, Any]) -> None:
    ports = _all_ports(compose)
    assert ports
    for entry in ports:
        assert long_form_problems(entry) == [], entry
    assert long_form_problems("127.0.0.1:5432:5432")
    assert long_form_problems(
        {"target": 5432, "published": "5432", "host_ip": "127.0.0.1", "protocol": "tcp"}
    )


def test_make_dev_uvicorn_keeps_loopback_default(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    recipe = re.search(r"^dev:.*?\n((?:\t.*\n)+)", makefile, flags=re.MULTILINE)
    assert recipe is not None
    body = recipe.group(1)
    assert "uvicorn app.main:app" in body
    assert "--host" not in body


def test_services_exactly_three(compose: dict[str, Any]) -> None:
    assert set(compose["services"]) == {"postgres", "minio", "minio-init"}


def test_minio_pinned_by_digest_with_site_command_ports_volume(
    compose: dict[str, Any], repo_root: Path
) -> None:
    service = compose["services"]["minio"]
    assert MINIO_IMAGE.fullmatch(service["image"])
    volumes = [str(item) for item in service["volumes"]]
    data_mounts = [item.split(":", 1)[1] for item in volumes if item.startswith("miniodata:")]
    assert data_mounts == [data_mounts[0]]
    path = data_mounts[0]
    assert _as_command(service["command"]) == ["server", path, "--console-address", ":9001"]
    assert service["ports"] == [
        {
            "target": 9000,
            "published": "${MINIO_API_HOST_PORT:-9000}",
            "host_ip": "127.0.0.1",
        },
        {
            "target": 9001,
            "published": "${MINIO_CONSOLE_HOST_PORT:-9001}",
            "host_ip": "127.0.0.1",
        },
    ]
    probe = service["healthcheck"]["test"]
    assert probe[0] == "CMD"
    assert probe[-1] == "--version"
    assert str(probe[1]).startswith("/")
    raw = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
    minio_block = raw.split("\n  minio:\n", 1)[1].split("\n  minio-init:", 1)[0]
    assert "CMD-SHELL" not in minio_block


def test_minio_credentials_interpolated_and_fail_loudly(
    compose: dict[str, Any], repo_root: Path
) -> None:
    minio = compose["services"]["minio"]["environment"]
    init = compose["services"]["minio-init"]["environment"]
    assert minio["MINIO_ROOT_USER"] == _ACCESS_INTERP
    assert minio["MINIO_ROOT_PASSWORD"] == _SECRET_INTERP
    assert init["MC_HOST_local"] == f"http://{_ACCESS_INTERP}:{_SECRET_INTERP}@minio:9000"
    raw = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
    assert ":?run cp .env.example .env" in raw
    assert ": " not in raw.split("MINIO_ROOT_USER:", 1)[1].split("\n", 1)[0]
    assert ": " not in raw.split("MINIO_ROOT_PASSWORD:", 1)[1].split("\n", 1)[0]
    assert "local_only_minio_root_user" not in raw
    assert "local_only_minio_root_password_32_chars" not in raw
    assert "minioadmin" not in raw


@pytest.mark.parametrize(
    ("restart", "healthcheck", "flagged"),
    [
        (None, False, True),
        ("unless-stopped", False, True),
        ("always", False, True),
        ("on-failure:0", False, True),
        ("on-failure:x", False, True),
        ("on-failure:", False, True),
        ("no", False, False),
        ("on-failure", False, False),
        ("on-failure:10", False, False),
    ],
)
def test_healthcheck_rule_flags_seeded_services(
    restart: str | None, healthcheck: bool, flagged: bool
) -> None:
    service: dict[str, Any] = {}
    if restart is not None:
        service["restart"] = restart
    if healthcheck:
        service["healthcheck"] = {"test": ["CMD", "/bin/true"]}
    assert _needs_healthcheck(service) is flagged


def test_init_creates_both_buckets_idempotently(compose: dict[str, Any], repo_root: Path) -> None:
    service = compose["services"]["minio-init"]
    assert MINIO_CLIENT_IMAGE.fullmatch(service["image"])
    assert service["depends_on"] == {"minio": {"condition": "service_healthy"}}
    assert service["restart"] == "on-failure:10"
    assert "ports" not in service
    names = canon_local_bucket_names(repo_root)
    example = (repo_root / ".env.example").read_text(encoding="utf-8")
    images = re.search(r"^GCS_BUCKET_IMAGES=(.+)$", example, re.MULTILINE)
    csv = re.search(r"^GCS_BUCKET_CSV=(.+)$", example, re.MULTILINE)
    submissions = re.search(r"^GCS_BUCKET_SUBMISSIONS=(.+)$", example, re.MULTILINE)  # S21
    assert images is not None and csv is not None and submissions is not None
    assert names == [images.group(1), csv.group(1), submissions.group(1)]
    assert _as_command(service["command"]) == [
        "mb",
        "--ignore-existing",
        *[f"local/{name}" for name in names],
    ]
    raw = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
    assert raw.count("local/marketplace-csv") == 1


def test_postgres_mounts_only_bootstrap_sql(compose: dict[str, Any]) -> None:
    """initdb runs 0000 only; role passwords are set by `make bootstrap-db` from verifiers."""
    assert compose["services"]["postgres"]["volumes"] == [
        "pgdata:/var/lib/postgresql/data",
        "./scripts/db/0000_bootstrap.sql:/docker-entrypoint-initdb.d/0000_bootstrap.sql:ro",
    ]
