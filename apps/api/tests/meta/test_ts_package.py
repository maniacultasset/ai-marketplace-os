"""S3 T17 (AC10, AC13): the `@maniacultasset/schemas` TypeScript package (FE alias `@mkos/schemas`)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

TS = Path("packages/schemas/ts")


def _package(repo_root: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((repo_root / TS / "package.json").read_text("utf-8"))
    return data


def test_ts_package_identity(repo_root: Path) -> None:
    pkg = _package(repo_root)
    assert pkg["name"] == "@maniacultasset/schemas"
    assert pkg["version"] == metadata.version("mkos-schemas") == "1.1.0"
    assert pkg["types"] == "src/index.ts"
    assert pkg["exports"] == {".": {"types": "./src/index.ts"}}
    assert pkg["files"] == ["src/api.d.ts", "src/contracts.d.ts", "src/index.ts"]
    assert pkg["publishConfig"] == {"registry": "https://npm.pkg.github.com"}
    assert "scripts" not in pkg
    assert "dependencies" not in pkg
    assert '"@mkos/schemas": "npm:@maniacultasset/schemas@' in pkg["description"]


def test_ts_version_matches_openapi(repo_root: Path) -> None:
    spec = json.loads((repo_root / "packages/schemas/jsonschema/openapi.json").read_text("utf-8"))
    assert spec["info"]["version"] == _package(repo_root)["version"]


def test_dev_deps_exact(repo_root: Path) -> None:
    dev = _package(repo_root)["devDependencies"]
    assert set(dev) == {"openapi-typescript", "json-schema-to-typescript", "typescript"}
    for name, version in dev.items():
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), (name, version)
    assert dev["openapi-typescript"].startswith("7.")
    lock = (repo_root / TS / "pnpm-lock.yaml").read_text("utf-8")
    for name, version in dev.items():
        assert f"{name}@{version}" in lock, name


def test_pnpm_never_runs_scripts_or_phones_home(repo_root: Path) -> None:
    # pnpm 12 reads only registry and auth keys from .npmrc; settings live in pnpm-workspace.yaml.
    settings = yaml.safe_load((repo_root / TS / "pnpm-workspace.yaml").read_text("utf-8"))
    assert settings["ignoreScripts"] is True
    assert settings["updateNotifier"] is False
    npmrc = (repo_root / TS / ".npmrc").read_text("utf-8")
    assert "@maniacultasset:registry=https://npm.pkg.github.com" in npmrc
    assert "_authToken" not in npmrc


def _no_npx_files(repo_root: Path) -> list[Path]:
    paths = [repo_root / "Makefile", repo_root / "packages/schemas/Makefile"]
    paths += sorted((repo_root / "scripts/ci").glob("*"))
    paths += sorted((repo_root / "scripts/schemas").glob("*"))
    paths += sorted((repo_root / ".github/workflows").glob("*.yml"))
    return [p for p in paths if p.is_file()]


def test_no_npx_anywhere(repo_root: Path) -> None:
    for path in _no_npx_files(repo_root):
        text = path.read_text("utf-8")
        assert not re.search(r"\bnpx\b|\bpnpm dlx\b|\bpnpx\b", text), path


def test_index_reexports_api_and_contracts(repo_root: Path) -> None:
    index = (repo_root / TS / "src/index.ts").read_text("utf-8")
    lines = [line for line in index.splitlines() if line and not line.startswith("//")]
    assert lines == ['export type * from "./api";', 'export type * from "./contracts";']


def test_generated_types_committed(repo_root: Path) -> None:
    api = (repo_root / TS / "src/api.d.ts").read_text("utf-8")
    contracts = (repo_root / TS / "src/contracts.d.ts").read_text("utf-8")
    assert "export interface paths" in api
    assert '"/api/v1/products"' in api
    assert "ErrorEnvelope" in api
    for name in ("CanonicalListing", "ErrorReport", "JobEnvelope", "ErrorCode"):
        assert re.search(rf"^export (interface|type) {name}\b", contracts, re.M), name
    assert not re.search(r"^export type ErrorCode\d", contracts, re.M)


@pytest.fixture
def bundle(script: Callable[[str], ModuleType]) -> ModuleType:
    return script("scripts/schemas/bundle_contracts.py")


def _write(directory: Path, name: str, schema: dict[str, Any]) -> None:
    (directory / f"{name}.schema.json").write_text(json.dumps(schema), "utf-8")


def test_bundle_collision_fails(tmp_path: Path, bundle: ModuleType) -> None:
    _write(tmp_path, "A", {"title": "A", "$defs": {"Shared": {"type": "string"}}})
    _write(tmp_path, "B", {"title": "B", "$defs": {"Shared": {"type": "integer"}}})
    with pytest.raises(bundle.BundleError, match="Shared"):
        bundle.bundle(tmp_path)


def test_bundle_same_def_twice_is_fine(tmp_path: Path, bundle: ModuleType) -> None:
    _write(tmp_path, "A", {"title": "A", "$defs": {"Shared": {"type": "string"}}})
    _write(tmp_path, "B", {"title": "B", "$defs": {"Shared": {"type": "string"}}})
    assert set(bundle.bundle(tmp_path)["$defs"]) == {"A", "B", "Shared"}


def test_bundle_has_no_remote_refs(tmp_path: Path, bundle: ModuleType) -> None:
    _write(tmp_path, "A", {"title": "A", "properties": {"x": {"$ref": "https://evil.example/x"}}})
    with pytest.raises(bundle.BundleError, match="remote"):
        bundle.bundle(tmp_path)


def test_bundle_of_the_repo(repo_root: Path, bundle: ModuleType) -> None:
    first = bundle.bundle(repo_root / "packages/schemas/jsonschema")
    second = bundle.bundle(repo_root / "packages/schemas/jsonschema")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    text = json.dumps(first)
    assert '"$id"' not in text
    assert '"$schema"' not in text
    assert "openapi" not in {k.lower() for k in first["$defs"]}
    for name in ("CanonicalListing", "ErrorReport", "ErrorCode"):
        assert first["$defs"][name]["title"] == name


def test_bundle_keeps_a_property_named_title(tmp_path: Path, bundle: ModuleType) -> None:
    _write(
        tmp_path,
        "A",
        {"title": "A", "properties": {"title": {"title": "Title", "type": "string"}}},
    )
    out = bundle.bundle(tmp_path)["$defs"]["A"]
    assert out["properties"] == {"title": {"type": "string"}}


def test_pack_checker(script: Callable[[str], ModuleType], monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    check = script("scripts/schemas/check_pack.py")
    good = {"files": [{"path": p} for p in sorted(check.EXPECTED)]}
    monkeypatch.setattr("sys.stdin", io.StringIO("noise\n" + json.dumps(good)))
    assert check.main() == 0
    bad = {"files": [*good["files"], {"path": "src/secret.ts"}]}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(bad)))
    assert check.main() == 1


def test_ts_consumer_check_in_schemas_drift(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text("utf-8")
    recipe = makefile.split("\nschemas-drift:\n", 1)[1].split("\n\n", 1)[0]
    lines = [line.strip() for line in recipe.splitlines()]
    assert lines[0] == "$(MAKE) -C packages/schemas all"
    assert "$(MAKE) -C packages/schemas ts-pack-check" in lines
    assert "$(MAKE) -C packages/schemas ts-consumer-check" in lines
    assert lines.index("$(MAKE) -C packages/schemas ts-consumer-check") > lines.index(
        "git diff --exit-code -- packages/schemas/jsonschema packages/schemas/ts"
    )
