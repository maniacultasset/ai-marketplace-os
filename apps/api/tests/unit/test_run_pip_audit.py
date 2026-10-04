"""pip-audit runner argv: OSV mode and a 60s timeout (ADR-0013 §6)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def audit(script: Callable[[str], ModuleType]) -> ModuleType:
    return script("scripts/ci/run_pip_audit.py")


class _Recorder:
    def __init__(self, audit_rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.audit_rc = audit_rc

    def __call__(
        self,
        argv: list[str],
        *args: object,
        check: bool = False,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        self.calls.append(command)
        if command and command[0] == "uv":
            return subprocess.CompletedProcess(command, 0)
        return subprocess.CompletedProcess(command, self.audit_rc)


def _install_recorder(
    audit: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    ignore_text: str = "GHSA-aaaa-bbbb-cccc 2999-01-01 reason\n",
    audit_rc: int = 0,
) -> _Recorder:
    ignore = tmp_path / "pip-audit-ignore.txt"
    ignore.write_text(ignore_text, encoding="utf-8")
    monkeypatch.setattr(audit, "IGNORE_FILE", ignore)
    recorder = _Recorder(audit_rc=audit_rc)
    monkeypatch.setattr(audit.subprocess, "run", recorder)
    return recorder


def test_pip_audit_argv_uses_osv_with_timeout(
    audit: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _install_recorder(audit, monkeypatch, tmp_path)
    assert audit.main() == 0
    assert len(recorder.calls) == 2
    export_argv, audit_argv = recorder.calls
    assert export_argv[0] == "uv"
    assert "--no-emit-workspace" in export_argv
    assert "--frozen" in export_argv
    assert ["-s", "osv"] in [audit_argv[i : i + 2] for i in range(len(audit_argv) - 1)]
    assert ["--timeout", "60"] in [audit_argv[i : i + 2] for i in range(len(audit_argv) - 1)]
    assert "--strict" in audit_argv
    assert "--disable-pip" in audit_argv
    assert audit_argv.count("-r") == 2
    export_path = export_argv[export_argv.index("-o") + 1]
    assert export_path in audit_argv
    constraints = str(audit.ROOT / "build-constraints.txt")
    assert constraints in audit_argv
    assert ["--ignore-vuln", "GHSA-aaaa-bbbb-cccc"] in [
        audit_argv[i : i + 2] for i in range(len(audit_argv) - 1)
    ]


def test_main_returns_pip_audit_exit_code(
    audit: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fail = _install_recorder(audit, monkeypatch, tmp_path, audit_rc=1)
    assert audit.main() == 1
    assert fail.calls[-1][0] != "uv"
    ok = _install_recorder(audit, monkeypatch, tmp_path, audit_rc=0)
    assert audit.main() == 0
    assert ok.calls[-1][0] != "uv"


def test_uv_export_failure_stops_before_audit(
    audit: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ignore = tmp_path / "pip-audit-ignore.txt"
    ignore.write_text("GHSA-aaaa-bbbb-cccc 2999-01-01 reason\n", encoding="utf-8")
    monkeypatch.setattr(audit, "IGNORE_FILE", ignore)
    calls: list[list[str]] = []

    def boom(argv: list[str], *args: object, check: bool = False, **kwargs: object) -> None:
        calls.append(list(argv))
        raise subprocess.CalledProcessError(2, argv)

    monkeypatch.setattr(audit.subprocess, "run", boom)
    with pytest.raises(subprocess.CalledProcessError):
        audit.main()
    assert len(calls) == 1
    assert calls[0][0] == "uv"


def test_expired_ignore_entry_stops_before_any_subprocess(
    audit: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _install_recorder(
        audit,
        monkeypatch,
        tmp_path,
        ignore_text="GHSA-aaaa-bbbb-cccc 2020-01-01 expired\n",
    )
    with pytest.raises(ValueError, match="expired"):
        audit.main()
    assert recorder.calls == []
