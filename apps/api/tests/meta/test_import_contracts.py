"""S15 T4 (DW7, AC12): the decrypt path (`app.adapters.credentials`) is importable only from
adapters and workers, directly or indirectly (EXECUTION-PLAN decision 15)."""

from __future__ import annotations

import configparser
from pathlib import Path

CONTRACT = "importlinter:contract:decrypt-path-only-in-adapters-and-workers"
SOURCES = {
    "app.api",
    "app.services",
    "app.repositories",
    "app.cli",
    "app.core",
    "app.db",
    "app.main",
}


def _lines(value: str) -> set[str]:
    return {line.strip() for line in value.splitlines() if line.strip()}


def test_decrypt_path_contract_present(repo_root: Path) -> None:
    parser = configparser.ConfigParser()
    parser.read(repo_root / ".importlinter", encoding="utf-8")
    assert CONTRACT in parser.sections()
    contract = parser[CONTRACT]
    assert contract["type"] == "forbidden"
    assert _lines(contract["source_modules"]) == SOURCES
    assert _lines(contract["forbidden_modules"]) == {"app.adapters.credentials"}
    assert contract.get("allow_indirect_imports", "False").strip() in ("False", "false")
    assert "ignore_imports" not in contract
