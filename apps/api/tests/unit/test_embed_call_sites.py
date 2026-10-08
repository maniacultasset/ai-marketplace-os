"""S16 T21 (CP12; invariant 7): an `embed(...)` call appears only in
`app/services/embeddings.py`, whose metered `embed_texts`/`embed_query` write the `agent_runs`
row, so no port or later caller can embed without one."""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
ALLOWED = APP / "services" / "embeddings.py"


def embed_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "embed"
    ]


def test_embed_called_only_in_embeddings_module() -> None:
    found = {
        str(path.relative_to(APP)): lines
        for path in sorted(APP.rglob("*.py"))
        if path != ALLOWED and (lines := embed_calls(path))
    }
    assert found == {}
    assert embed_calls(ALLOWED)  # the metered call itself


def test_call_site_scan_sees_a_seeded_call(tmp_path: Path) -> None:
    seeded = tmp_path / "pipeline_deps.py"
    seeded.write_text("async def f(self, q):\n    return await self._embedder.embed([q])\n")
    assert embed_calls(seeded) == [2]
