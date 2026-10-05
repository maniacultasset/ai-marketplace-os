#!/usr/bin/env python3
"""Golden fixture checks for the contracts gate (stdlib + mkos_schemas only)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mkos_schemas.export import MODELS
from mkos_schemas.images import ImageReport


def _fail(reason: str) -> int:
    print(f"contracts_check: {reason}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="contracts_check")
    parser.add_argument("--fixtures", required=True, type=Path)
    args = parser.parse_args(argv)
    fixtures_dir = args.fixtures
    if not fixtures_dir.is_dir():
        return _fail("no golden fixtures")
    paths = sorted(fixtures_dir.glob("*.json"))
    if not paths:
        return _fail("no golden fixtures")
    by_stem = {p.stem: p for p in paths}
    for model in MODELS:
        name = model.__name__
        path = by_stem.get(name)
        if path is None:
            return _fail(f"missing golden fixture: {name}")
    for path in paths:
        if path.stem not in {m.__name__ for m in MODELS}:
            return _fail(f"orphan fixture: {path.name}")
    for model in MODELS:
        path = by_stem[model.__name__]
        raw = json.loads(path.read_text(encoding="utf-8"))
        try:
            obj = model.model_validate(raw)
        except Exception:
            return _fail(f"invalid fixture: {model.__name__}")
        dumped = obj.model_dump(by_alias=True, mode="json")
        if dumped != raw:
            return _fail(f"round-trip changed: {model.__name__}")
    schema = json.dumps(ImageReport.model_json_schema(mode="serialization"))
    if "pass_" in schema:
        return _fail("pass_ emitted")
    if "pass" not in schema:
        return _fail("pass_ emitted")
    dumped = ImageReport.model_validate(
        {
            "image_id": "11111111-1111-1111-1111-111111111111",
            "marketplace": "amazon",
            "slot": "main",
            "pass": True,
            "processed_at": "2026-01-01T00:00:00Z",
        }
    ).model_dump(by_alias=True, mode="json")
    if "pass_" in json.dumps(dumped):
        return _fail("pass_ emitted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
