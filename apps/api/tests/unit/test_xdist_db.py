"""tests/support/xdist_db.py: the per-worker copies of mos_test and the allowed-database names."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.support import xdist_db
from tests.support.xdist_db import is_test_database, use_mutant_database, use_worker_database


@pytest.mark.parametrize(
    "name",
    ["mos_test", "mos_iso", "mos_test_gw0", "mos_test_gw12", "mos_test_mut0", "mos_test_mut7"],
)
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
        "mos_test_mut",
        "mos_test_mutx",
        "mos_test_mut1;",
        "mos_iso_mut0",
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


def test_mutant_run_off_mos_test_is_never_redirected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """mos_iso, the dev database, an xdist copy or no URL: no slot taken, nothing connects."""
    for db in ("mos_iso", "marketplace_os", "mos_test_gw0"):
        url = f"postgresql+psycopg://u:p@127.0.0.1:1/{db}"
        monkeypatch.setenv("DATABASE_URL", url)
        assert use_mutant_database(tmp_path / "slots") is None
        assert url == os.environ["DATABASE_URL"]
    monkeypatch.delenv("DATABASE_URL")
    assert use_mutant_database(tmp_path / "slots") is None
    assert not (tmp_path / "slots").exists()


def test_mutant_slots_are_exclusive_per_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A slot another process holds is skipped; this process keeps its own slot; a forked child
    (another pid with the parent's entry) takes a slot of its own."""
    slots = tmp_path / "slots"
    slots.mkdir()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys, time\n"
            f"h = open({str(slots / '0.lock')!r}, 'a')\n"
            "fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "print('held', flush=True)\n"
            "time.sleep(60)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    held = None
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        monkeypatch.setattr(xdist_db, "_held_slot", None)
        assert xdist_db._take_slot(slots) == 1
        assert xdist_db._take_slot(slots) == 1
        held = xdist_db._held_slot
        assert held is not None
        monkeypatch.setattr(xdist_db, "_held_slot", (held[0] + 1, held[1], held[2]))
        assert xdist_db._take_slot(slots) == 2
        with open(slots / "1.lock", "a") as other, pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        holder.kill()
        holder.wait()
        for entry in (held, xdist_db._held_slot):
            if entry is not None:
                entry[2].close()
        monkeypatch.setattr(xdist_db, "_held_slot", None)


def test_drop_mutant_databases_needs_its_one_command() -> None:
    assert xdist_db.main([]) == 2
    assert xdist_db.main(["drop"]) == 2
