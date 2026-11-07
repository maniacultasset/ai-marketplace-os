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
    [
        "mos_test",
        "mos_iso",
        "mos_test_gw0",
        "mos_test_gw12",
        "mos_test_mut0",
        "mos_test_mut7",
        "mos_test_lsi4",
        "mos_test_l0",
        "mos_test_l" + "a" * 16,
        "mos_test_lsi4_gw0",
        "mos_test_lsi4_gw12",
        "mos_test_l" + "z9" * 8 + "_gw3",
    ],
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
        "prod",
        # lane templates and their copies: exact names only
        "mos_test_l",
        "mos_test_l_gw0",
        "mos_test_lSI4",
        "mos_test_LSI4",
        "mos_test_l" + "a" * 17,
        "mos_test_l" + "a" * 17 + "_gw0",
        "mos_test_lsi-4",
        "mos_test_lsi_4",
        "mos_test_lsi4\n",
        "mos_test_lsi4x_gw",
        "mos_test_lsi4_gw",
        "mos_test_lsi4_gwx",
        "mos_test_lsi4_gw1_extra",
        "mos_test_lsi4_gw1x",
        "mos_test_lsi4_mut0",
        "xmos_test_lsi4",
        "mos_iso_lsi4",
        None,
        1,
    ],
)
def test_refused_databases(name: object) -> None:
    assert not is_test_database(name)


@pytest.mark.parametrize(
    ("template", "worker", "copy"),
    [
        ("mos_test", "gw0", "mos_test_gw0"),
        ("mos_test", "gw11", "mos_test_gw11"),
        ("mos_test_lsi4", "gw0", "mos_test_lsi4_gw0"),
        ("mos_test_lproof", "gw3", "mos_test_lproof_gw3"),
    ],
)
def test_worker_copy_is_named_after_its_template(template: str, worker: str, copy: str) -> None:
    assert xdist_db.worker_database_name(template, worker) == copy
    assert is_test_database(copy)
    assert xdist_db.worker_copies(template).fullmatch(copy)


@pytest.mark.parametrize("template", ["marketplace_os", "mos_iso", "mos_test_gw0", "mos_test_l"])
def test_worker_copies_need_a_template(template: str) -> None:
    with pytest.raises(ValueError, match="template"):
        xdist_db.worker_copies(template)


def test_worker_copy_needs_a_worker_id() -> None:
    with pytest.raises(RuntimeError, match="worker id"):
        xdist_db.worker_database_name("mos_test_lsi4", "gw1_extra")


SERVER = [
    "marketplace_os",
    "postgres",
    "prod",
    "mos_iso",
    "mos_test",
    "mos_test_gw0",
    "mos_test_gw1",
    "mos_test_mut0",
    "mos_test_mut904",
    "mos_test_lsi",
    "mos_test_lsi_gw0",
    "mos_test_lsi4",
    "mos_test_lsi4_gw0",
    "mos_test_lsi4_gw1",
    "mos_test_lsi4x",
    "mos_test_lsi4x_gw1",
    "mos_test_lsi4_gw1_extra",
    "mos_scratch_0a1b2c3d",
]


def test_lane_controller_drops_only_its_own_copies() -> None:
    """A lane drops its own `_gwN` copies: never mos_test's, another lane's (even one whose tag
    shares a prefix), a slot copy, or anything else on the server."""
    lane = xdist_db.worker_copies("mos_test_lsi4")
    assert xdist_db.copies_to_drop("mos_test_lsi4", SERVER, lane) == [
        "mos_test_lsi4_gw0",
        "mos_test_lsi4_gw1",
    ]
    # Even a pattern wider than the lane's own (any worker copy, the slot copies) stays scoped.
    assert xdist_db.copies_to_drop("mos_test_lsi4", SERVER, xdist_db.WORKER_DB) == [
        "mos_test_lsi4_gw0",
        "mos_test_lsi4_gw1",
    ]
    assert xdist_db.copies_to_drop("mos_test_lsi4", SERVER, xdist_db.MUTANT_DB) == []
    assert xdist_db.copies_to_drop("mos_test_lsi", SERVER, xdist_db.WORKER_DB) == [
        "mos_test_lsi_gw0"
    ]


def test_mos_test_controller_never_drops_a_lane_copy() -> None:
    own = xdist_db.worker_copies("mos_test")
    assert xdist_db.copies_to_drop("mos_test", SERVER, own) == ["mos_test_gw0", "mos_test_gw1"]
    assert xdist_db.copies_to_drop("mos_test", SERVER, xdist_db.WORKER_DB) == [
        "mos_test_gw0",
        "mos_test_gw1",
    ]
    assert xdist_db.copies_to_drop("mos_test", SERVER, xdist_db.MUTANT_DB) == [
        "mos_test_mut0",
        "mos_test_mut904",
    ]


def test_every_name_a_controller_drops_passes_the_drop_guard() -> None:
    for template in ("mos_test", "mos_test_lsi4", "mos_test_lsi"):
        for pattern in (xdist_db.worker_copies(template), xdist_db.WORKER_DB, xdist_db.MUTANT_DB):
            for name in xdist_db.copies_to_drop(template, SERVER, pattern):
                assert xdist_db.WORKER_DB.fullmatch(name) or xdist_db.MUTANT_DB.fullmatch(name)


@pytest.mark.parametrize("db", ["mos_test", "mos_test_lsi4"])
def test_drop_refuses_a_template_or_other_database(db: str) -> None:
    """`_drop` asserts a copy name before any SQL runs (the connection is never used)."""
    with pytest.raises(AssertionError):
        xdist_db._drop(None, db)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url",
    [
        None,
        "postgresql+psycopg://u:p@127.0.0.1:1/mos_iso",
        "postgresql+psycopg://u:p@127.0.0.1:1/marketplace_os",
        "postgresql+psycopg://u:p@127.0.0.1:1/mos_test_gw0",
        "postgresql+psycopg://u:p@127.0.0.1:1/mos_test_lsi4_gw0",
        "postgresql+psycopg://u:p@127.0.0.1:1/mos_test_l",
    ],
)
def test_controller_off_a_template_drops_nothing(url: str | None) -> None:
    """Off a template nothing connects (port 1 would refuse the connection and raise)."""
    xdist_db.drop_worker_databases(url)
    xdist_db.drop_worker_databases(url, xdist_db.MUTANT_DB)


def test_lane_controller_never_drops_slot_copies() -> None:
    """Slot copies belong to mos_test: on a lane template the mutant drop never connects."""
    xdist_db.drop_worker_databases(
        "postgresql+psycopg://u:p@127.0.0.1:1/mos_test_lsi4", xdist_db.MUTANT_DB
    )


def test_no_copy_outside_a_worker_or_off_mos_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serial runs, mos_iso and the dev database are never redirected (and nothing connects)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/mos_test")
    assert use_worker_database(None) is None
    for db in ("mos_iso", "marketplace_os", "mos_test_gw0", "mos_test_lsi4_gw0", "mos_test_l"):
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
    for db in ("mos_iso", "marketplace_os", "mos_test_gw0", "mos_test_lsi4", "mos_test_lsi4_gw0"):
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
