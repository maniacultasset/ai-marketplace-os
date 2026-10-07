"""S13 B5a (§4 T0 rule): one Alembic head, and the facts_guard revision chained to the ledger
row before it (CANON.md "Migration ledger"), never to an assumed number."""

from __future__ import annotations

import re

from alembic.script import ScriptDirectory
from app.db.migrations import alembic_config

from .conftest import REPO_ROOT

LEDGER_ROW = re.compile(r"^\| `(\d{4}[a-z]?_[a-z_]+)` \|", re.M)


def _ledger() -> list[str]:
    text = (REPO_ROOT / "docs/canon/CANON.md").read_text(encoding="utf-8")
    section = text.split("## Migration ledger", 1)[1].split("\n## ", 1)[0]
    return LEDGER_ROW.findall(section)


def test_mig_single_head() -> None:
    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    assert len(heads) == 1, heads


def test_facts_guard_down_revision_is_ledger_head() -> None:
    script = ScriptDirectory.from_config(alembic_config())
    revision = script.get_revision("0006_facts_guard")
    assert revision is not None
    ledger = _ledger()
    assert "0006_facts_guard" in ledger
    assert revision.down_revision == ledger[ledger.index("0006_facts_guard") - 1]
    # every revision file is claimed in the ledger
    claimed = set(ledger)
    for rev in script.walk_revisions():
        assert rev.revision in claimed, rev.revision
