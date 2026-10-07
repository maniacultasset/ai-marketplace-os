"""S16 F3-F7 (D1), closed by the S13b rebase: a deployed api (staging or production) gets the
S3-API `ObjectStorage` for brand documents, pointed at the configured endpoint, so
`POST /api/v1/brand/documents` no longer fails closed for want of a store."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.deployed_env import DEPLOYED_S15_ENV

LOCAL_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os"
DSN = "https://public@o0.ingest.example.invalid/0"


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_deployed_api_gets_object_storage(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    from app.adapters.storage import ObjectStorage, get_storage
    from app.core.config import get_settings
    from app.services.brand_storage import brand_bucket, get_document_store, use_document_store

    env = {
        "APP_ENV": app_env,
        "APP_ROLE": "api",
        "DATABASE_URL": LOCAL_URL,
        "SENTRY_DSN": DSN,
        "JWT_SECRET": "Zq9-not-committed-private-secret-32+",
        **DEPLOYED_S15_ENV,
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    use_document_store(None)
    get_settings.cache_clear()
    get_storage.cache_clear()
    try:
        store = get_document_store()
        assert isinstance(store, ObjectStorage)
        assert store._client.meta.endpoint_url == DEPLOYED_S15_ENV["S3_ENDPOINT_URL"]
        assert brand_bucket() == get_settings().gcs_bucket_csv
    finally:
        get_storage.cache_clear()
        get_settings.cache_clear()
