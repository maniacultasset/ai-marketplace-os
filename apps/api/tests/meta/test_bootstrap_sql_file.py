"""AC8: scripts/db/0000_bootstrap.sql is CANON-ADDENDUM.md §1's SQL, byte for byte (CANON.md:92)."""

from __future__ import annotations

import re
from pathlib import Path


def _addendum_sql(repo_root: Path) -> str:
    addendum = (repo_root / "docs/canon/CANON-ADDENDUM.md").read_text(encoding="utf-8")
    section = addendum[addendum.index("## 1. Bootstrap SQL") : addendum.index("## 2. Bootstrap")]
    block = re.search(r"```sql\n(.*?)```", section, flags=re.DOTALL)
    assert block is not None
    return block.group(1)


def test_file_equals_addendum_block(repo_root: Path) -> None:
    actual = (repo_root / "scripts/db/0000_bootstrap.sql").read_text(encoding="utf-8")
    assert actual == _addendum_sql(repo_root)
    assert actual.endswith(";\n")
