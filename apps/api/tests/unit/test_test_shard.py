"""S26 (KNOWN-ISSUES §E): the two api-tests CI shards are disjoint, complete and stable."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import REPO_ROOT
from tests.support.shard import SPLIT_DIRS, bucket, parse, shard_of


def _test_files() -> list[str]:
    roots = [REPO_ROOT / "apps/api/tests", *(REPO_ROOT / "packages").glob("*/tests")]
    files = [p for root in roots for p in root.rglob("test_*.py")]
    return sorted(p.relative_to(REPO_ROOT).as_posix() for p in files)


def test_shard_every_file_in_exactly_one_shard() -> None:
    files = _test_files()
    assert len(files) > 400
    shards = {f: shard_of(f) for f in files}
    assert set(shards.values()) == {1, 2}
    one = {f for f, s in shards.items() if s == 1}
    two = {f for f, s in shards.items() if s == 2}
    assert one | two == set(files) and not one & two


def test_shard_fast_suites_stay_in_shard_one() -> None:
    for folder in ("unit", "meta", "adapters"):
        for path in (REPO_ROOT / "apps/api/tests" / folder).rglob("test_*.py"):
            assert shard_of(path.relative_to(REPO_ROOT).as_posix()) == 1, path
    for path in (REPO_ROOT / "packages").glob("*/tests/test_*.py"):
        assert shard_of(path.relative_to(REPO_ROOT).as_posix()) == 1, path


def test_shard_split_dirs_are_shared_both_ways() -> None:
    split = [f for f in _test_files() if f.startswith(SPLIT_DIRS)]
    ones = sum(shard_of(f) == 1 for f in split)
    # both shards run a real share of the database suites (neither is empty or trivial)
    assert 0.2 * len(split) < ones < 0.6 * len(split), (ones, len(split))


def test_shard_bucket_is_stable() -> None:
    path = "apps/api/tests/integration/test_publish_flow.py"
    assert bucket(path) == bucket(str(Path(path)))
    assert 0 <= bucket(path) < 100


@pytest.mark.parametrize(("raw", "want"), [(None, None), ("", None), ("1", 1), (" 2 ", 2)])
def test_shard_parse(raw: str | None, want: int | None) -> None:
    assert parse(raw) == want


@pytest.mark.parametrize("raw", ["0", "3", "1/2", "a"])
def test_shard_parse_refuses_other_values(raw: str) -> None:
    with pytest.raises(ValueError, match="API_TEST_SHARD"):
        parse(raw)
