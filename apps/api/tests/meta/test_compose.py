"""AC9: docker-compose.yml (CANON-ADDENDUM.md §1 postgres, pinned by digest, loopback ports).

The MinIO services are pending the image decision (DEVIATIONS.md): minio/minio and minio/mc are
no longer published, so their three planned tests land with them.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

DIGEST_IMAGE = re.compile(r"^pgvector/pgvector:0\.8\.5-pg17@sha256:[0-9a-f]{64}$")


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


def test_no_version_key(compose: dict[str, Any]) -> None:
    assert "version" not in compose


def test_postgres_matches_addendum_with_pg17_image_and_loopback_port(
    compose: dict[str, Any], repo_root: Path
) -> None:
    actual = dict(compose["services"]["postgres"])
    expected = _addendum_postgres(repo_root)
    assert DIGEST_IMAGE.fullmatch(actual.pop("image"))
    assert actual.pop("ports") == ["127.0.0.1:5432:5432"]
    expected.pop("image")
    expected.pop("ports")
    assert actual == expected


def test_long_running_services_have_healthchecks(compose: dict[str, Any]) -> None:
    for name, service in compose["services"].items():
        if service.get("restart") != "no":
            assert "healthcheck" in service, name


def test_named_volumes(compose: dict[str, Any]) -> None:
    declared = set(compose.get("volumes") or {})
    assert "pgdata" in declared
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


def test_make_dev_uvicorn_keeps_loopback_default(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    recipe = re.search(r"^dev:.*?\n((?:\t.*\n)+)", makefile, flags=re.MULTILINE)
    assert recipe is not None
    body = recipe.group(1)
    assert "uvicorn app.main:app" in body
    assert "--host" not in body
