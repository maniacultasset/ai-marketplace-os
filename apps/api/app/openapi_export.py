"""Write the committed `/api/v1` contract: `packages/schemas/jsonschema/openapi.json` (CANON.md:43).

Run by `make -C packages/schemas openapi` under a fixed local env (CANON-ADDENDUM.md §13, corrected
by ADR-0004 §3: no asyncpg, no REDIS_URL). Building the app opens no database connection. The dump
is sorted and has no `servers`, so the bytes depend only on the code: the drift gate compares them.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def build() -> dict[str, Any]:
    from app.main import create_app

    spec = create_app().openapi()
    spec.pop("servers", None)
    return spec


def render(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def export(out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(build()), encoding="utf-8")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.openapi_export")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)
    export(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
