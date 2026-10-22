"""The committed platform templates file the migrate job loads (`templates load` with no
`--file`) is generated from the Python mappings (`seed_ops.platform_template_loads()`, the Amazon
and Flipkart mappings `seed` and `platform load` write). The two never drift."""

from __future__ import annotations

from pathlib import Path

REGENERATE = "uv run --frozen python -m app.db.seed_ops"


def test_platform_templates_path_ships_in_the_image(repo_root: Path) -> None:
    from app.cli import PLATFORM_TEMPLATES_PATH

    expected = repo_root / "apps/api/app/db/seed_data/platform_templates.json"
    assert expected == PLATFORM_TEMPLATES_PATH
    assert PLATFORM_TEMPLATES_PATH.is_file()


def test_platform_templates_file_matches_the_python() -> None:
    from app.cli import PLATFORM_TEMPLATES_PATH
    from app.db.platform_loads import load_template_file
    from app.db.seed_ops import platform_template_loads, render_platform_templates

    stale = f"{PLATFORM_TEMPLATES_PATH.name} is stale; regenerate it with `{REGENERATE}`"
    assert PLATFORM_TEMPLATES_PATH.read_text(encoding="utf-8") == render_platform_templates(), stale
    assert load_template_file(PLATFORM_TEMPLATES_PATH) == platform_template_loads(), stale


def test_platform_templates_are_the_amazon_and_flipkart_mappings() -> None:
    from app.db.seed_ops import (
        amazon_template_loads,
        flipkart_template_loads,
        platform_template_loads,
    )

    assert platform_template_loads() == amazon_template_loads() + flipkart_template_loads()
    keys = [(t.template.marketplace.value, t.template.category) for t in platform_template_loads()]
    assert len(keys) == len(set(keys))  # one version per pair, so one promotion each
