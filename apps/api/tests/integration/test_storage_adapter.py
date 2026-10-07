"""S13 B1 (AC 20; PD8): the object storage adapter against the compose MinIO, the CSV key
literal, the pinned dependency and the one-module import rule."""

from __future__ import annotations

import ast
import inspect
import re
import tomllib
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.adapters.storage import (
    ObjectStorage,
    StorageNotFound,
    csv_bucket,
    get_storage,
    import_object_key,
    new_storage,
)

from .conftest import LOCAL_HOSTS, REPO_ROOT

BOTO3_PIN = re.compile(r'"boto3==(\d+\.\d+\.\d+)"')


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ObjectStorage]:
    from app.core.config import get_settings

    monkeypatch.chdir(tmp_path)  # a developer's .env must not leak in
    get_settings.cache_clear()
    get_storage.cache_clear()
    yield new_storage()
    get_storage.cache_clear()
    get_settings.cache_clear()


@pytest.mark.allow_hosts(LOCAL_HOSTS)
async def test_storage_round_trip(storage: ObjectStorage) -> None:
    key = import_object_key(uuid.uuid4(), uuid.uuid4())
    data = b"seller_sku,product_name\nA-1,Kurta\n"
    await storage.put(csv_bucket(), key, data, "text/csv")
    assert await storage.get(csv_bucket(), key) == data


@pytest.mark.allow_hosts(LOCAL_HOSTS)
async def test_storage_missing_key_not_found(storage: ObjectStorage) -> None:
    key = import_object_key(uuid.uuid4(), uuid.uuid4())
    with pytest.raises(StorageNotFound) as caught:
        await storage.get(csv_bucket(), key)
    assert key not in str(caught.value)
    assert csv_bucket() not in str(caught.value)


def test_import_object_key_literal() -> None:
    tenant = uuid.UUID("11111111-2222-3333-4444-555555555555")
    upload = uuid.UUID("66666666-7777-8888-9999-000000000000")
    assert import_object_key(tenant, upload) == (
        "11111111-2222-3333-4444-555555555555/imports/66666666-7777-8888-9999-000000000000.csv"
    )
    with pytest.raises(ValueError, match="UUIDs"):
        import_object_key("../other", upload)  # type: ignore[arg-type]


def test_storage_repr_has_no_secret(storage: ObjectStorage) -> None:
    from app.core.config import get_settings

    settings = get_settings()
    shown = repr(storage) + str(storage)
    assert settings.s3_secret_key not in shown
    assert settings.s3_access_key not in shown
    assert "s3_secret_key" not in repr(settings)


def test_storage_get_two_positional_args() -> None:
    """The exact call shape the import runner uses, `get(bucket, key)`: later steps may add only
    defaulted keyword-only parameters."""
    params = list(inspect.signature(ObjectStorage.get).parameters.values())[1:]
    positional = [p for p in params if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD]
    assert [p.name for p in positional] == ["bucket", "key"]
    for extra in params[2:]:
        assert extra.kind is inspect.Parameter.KEYWORD_ONLY
        assert extra.default is not inspect.Parameter.empty
    put = list(inspect.signature(ObjectStorage.put).parameters)[1:5]
    assert put == ["bucket", "key", "data", "content_type"]


def test_lock_pins_boto3() -> None:
    manifest = (REPO_ROOT / "apps/api/pyproject.toml").read_text(encoding="utf-8")
    pinned = BOTO3_PIN.findall(manifest)
    assert len(pinned) == 1, "boto3 must be pinned with == exactly once"
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    versions = {p["name"]: p["version"] for p in lock["package"]}
    assert versions["boto3"] == pinned[0]
    assert "botocore" in versions


def _imports_boto(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        if any(n.split(".")[0] in {"boto3", "botocore"} for n in names):
            return True
    return False


def test_only_adapter_imports_boto3() -> None:
    app_root = REPO_ROOT / "apps/api/app"
    importers = sorted(
        str(p.relative_to(app_root)) for p in app_root.rglob("*.py") if _imports_boto(p)
    )
    assert importers == ["adapters/storage.py"]
    contracts = (REPO_ROOT / ".importlinter").read_text(encoding="utf-8")
    assert "[importlinter:contract:boto3-only-in-storage-adapter]" in contracts
