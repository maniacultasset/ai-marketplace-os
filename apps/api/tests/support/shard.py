"""The two `api-tests` CI shards (S26; KNOWN-ISSUES §E "CI api-tests duration").

`make api-tests` runs shard 1 and `make api-tests-integration` runs shard 2, so each required
job stays well inside its timeout. `API_TEST_SHARD` unset runs everything (a local full run).

Shard 1 holds every test outside the database-heavy folders (`unit`, `meta`, `adapters`, the
packages' tests, the top-level modules) plus the database-heavy files whose stable bucket is
below `SPLIT`; shard 2 holds the other database-heavy files. The bucket is a CRC-32 of the
file's repo-relative path, so a file never moves between runs or machines, a new file lands in
exactly one shard, and the two shards are disjoint and complete by construction.
"""

from __future__ import annotations

import zlib
from pathlib import PurePosixPath
from typing import Final

SHARDS: Final = (1, 2)
# Database-heavy folders, split between the two shards by bucket.
SPLIT_DIRS: Final = ("apps/api/tests/integration/", "apps/api/tests/security/")
# The share (in percent) of the split folders' files that shard 1 also runs. 40 left shard 2 at
# 30 min on the runner against 19 for shard 1 (S22b, PR #47); 50 balances the test count.
SPLIT: Final = 50


def bucket(relpath: str) -> int:
    """A stable 0..99 bucket for a repo-relative POSIX path."""
    return zlib.crc32(PurePosixPath(relpath).as_posix().encode("utf-8")) % 100


def shard_of(relpath: str) -> int:
    """The shard (1 or 2) that runs the test file at `relpath` (repo-relative, POSIX)."""
    path = PurePosixPath(relpath).as_posix()
    if not path.startswith(SPLIT_DIRS):
        return 1
    return 1 if bucket(path) < SPLIT else 2


def parse(raw: str | None) -> int | None:
    """`API_TEST_SHARD` → 1, 2 or None (unset or empty: every test). Anything else is refused."""
    if raw is None or raw.strip() == "":
        return None
    value = raw.strip()
    if value not in {str(s) for s in SHARDS}:
        raise ValueError(f"API_TEST_SHARD must be 1 or 2, got {value!r}")
    return int(value)
