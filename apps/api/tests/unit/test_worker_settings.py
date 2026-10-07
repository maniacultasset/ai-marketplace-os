"""S13 A8 (PLAN D3, D6; KNOWN-ISSUES.md:36, :48, :49): the worker's code-constant settings and the
entrypoint's `worker` role (service name, the one exec, no port)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from app.workers import settings

ENTRYPOINT = Path(__file__).resolve().parents[2] / "docker-entrypoint.sh"


def test_worker_settings_literal() -> None:
    assert (
        settings.DEFAULT_CONCURRENCY,
        settings.PUBLISH_CONCURRENCY,
        settings.GRACE_S,
        settings.HEARTBEAT_S,
        settings.POLL_S,
        settings.STALLED_WORKER_S,
        settings.APP_POOL_MAX,
        settings.QUEUE_POOL_MAX,
    ) == (4, 2, 8, 10, 2, 30, 10, 5)
    assert settings.QUEUE_CONCURRENCY == (("default", 4), ("publish", 2))
    assert settings.GRACE_S < 10  # inside Cloud Run's 10 s SIGTERM window
    assert settings.WORKER_SERVICE_NAME == "marketplace-os-worker"


def _fake_python(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "python"
    fake.write_text(
        '#!/bin/sh\necho "APP_NAME=$APP_NAME"\necho "OTEL=$OTEL_SERVICE_NAME"\necho "ARGS=$*"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return bin_dir


def _run(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    bin_dir = _fake_python(tmp_path)
    full = {"PATH": f"{bin_dir}:/usr/bin:/bin", "APP_ROLE": "worker", **env}
    return subprocess.run(
        ["sh", str(ENTRYPOINT)], env=full, capture_output=True, text=True, check=False, timeout=30
    )


def test_entrypoint_worker_role_service_name(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "APP_NAME=marketplace-os-worker",
        "OTEL=marketplace-os-worker",
        "ARGS=-m app.workers.main",
    ]


def test_entrypoint_worker_role_keeps_explicit_names(tmp_path: Path) -> None:
    result = _run(tmp_path, APP_NAME="custom-a", OTEL_SERVICE_NAME="custom-o")
    assert result.stdout.splitlines()[:2] == ["APP_NAME=custom-a", "OTEL=custom-o"]


def test_entrypoint_worker_role_has_no_port() -> None:
    text = ENTRYPOINT.read_text(encoding="utf-8")
    block = text.split("  worker)", 1)[1].split(";;", 1)[0]
    assert "PORT" not in block and "uvicorn" not in block and "health" not in block
    assert "exec python -m app.workers.main" in block
    assert os.access(ENTRYPOINT, os.R_OK)
