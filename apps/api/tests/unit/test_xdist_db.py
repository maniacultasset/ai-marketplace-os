"""tests/support/xdist_db.py: the per-worker copies of mos_test and the allowed-database names."""

from __future__ import annotations

import os

import pytest
from tests.support.xdist_db import is_test_database, use_worker_database


@pytest.mark.parametrize("name", ["mos_test", "mos_iso", "mos_test_gw0", "mos_test_gw12"])
def test_allowed_test_databases(name: str) -> None:
    assert is_test_database(name)


@pytest.mark.parametrize(
    "name",
    [
        "marketplace_os",
        "postgres",
        "mos_test_gw",
        "mos_test_gwx",
        "mos_test_gw1x",
        "mos_test_gw1\n",
        "xmos_test_gw1",
        "mos_iso_gw0",
        "mos_test_",
        "mos_scratch_0a1b2c3d",
        "MOS_TEST",
        None,
        1,
    ],
)
def test_refused_databases(name: object) -> None:
    assert not is_test_database(name)


def test_no_copy_outside_a_worker_or_off_mos_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serial runs, mos_iso and the dev database are never redirected (and nothing connects)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/mos_test")
    assert use_worker_database(None) is None
    for db in ("mos_iso", "marketplace_os", "mos_test_gw0"):
        url = f"postgresql+psycopg://u:p@127.0.0.1:1/{db}"
        monkeypatch.setenv("DATABASE_URL", url)
        assert use_worker_database("gw0") is None
        assert url == os.environ["DATABASE_URL"]
    monkeypatch.delenv("DATABASE_URL")
    assert use_worker_database("gw0") is None


def test_unexpected_worker_id_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/mos_test")
    with pytest.raises(RuntimeError, match="worker id"):
        use_worker_database("gw0; DROP DATABASE x")
