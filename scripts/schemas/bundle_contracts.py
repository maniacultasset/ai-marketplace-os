"""Merge `jsonschema/*.schema.json` into one `$defs` bundle for `json2ts` (S3 T17).

Every model file becomes a `$defs` entry under its title, beside the defs it carries. A name that
appears twice with a different body fails the bundle (two models would silently share one TS type
otherwise). `$id` and `$schema` are dropped, so the bundle holds only local `#/$defs/...` refs and
the generator never resolves a URL; a remote `$ref` fails the bundle. Stdlib only.

Usage: bundle_contracts.py <jsonschema dir> <out file>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

TITLE = "MkosContracts"


class BundleError(ValueError):
    pass


def _remote_refs(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#/"):
            found.append(ref)
        for value in node.values():
            found.extend(_remote_refs(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_remote_refs(value))
    return found


def _strip_titles(node: Any) -> Any:
    """Drop every string `title` below a model, so json2ts names only the models themselves
    (pydantic titles each property; json2ts would export one alias per property otherwise).
    A property *named* `title` maps to a schema object, not a string, so it is kept. Types are
    unchanged: only names, defaults and descriptions next to a `$ref` go."""
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            # A `$ref` with siblings (pydantic's `default` on an enum field) would be copied into a
            # new named type (`ErrorCode1`, ...); the siblings never change the TS type.
            return {"$ref": node["$ref"]}
        return {
            k: _strip_titles(v)
            for k, v in node.items()
            if not (k == "title" and isinstance(v, str))
        }
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


def _model(body: dict[str, Any], name: str) -> dict[str, Any]:
    return {**_strip_titles(body), "title": name}


def _add(defs: dict[str, Any], name: str, body: Any, source: str) -> None:
    if name in defs and defs[name] != body:
        raise BundleError(f"bundle: {name!r} has two different bodies (second from {source})")
    defs[name] = body


def bundle(schema_dir: Path) -> dict[str, Any]:
    defs: dict[str, Any] = {}
    files = sorted(schema_dir.glob("*.schema.json"))
    if not files:
        raise BundleError(f"bundle: no *.schema.json in {schema_dir}")
    for path in files:
        schema = json.loads(path.read_text(encoding="utf-8"))
        nested = schema.pop("$defs", {})
        schema.pop("$id", None)
        schema.pop("$schema", None)
        title = schema.get("title") or path.name.removesuffix(".schema.json")
        for name, body in sorted(nested.items()):
            _add(defs, name, _model(body, name), path.name)
        _add(defs, title, _model(schema, title), path.name)
    out = {"title": TITLE, "type": "object", "$defs": dict(sorted(defs.items()))}
    remote = _remote_refs(out)
    if remote:
        raise BundleError(f"bundle: remote $ref not allowed: {sorted(set(remote))[:5]}")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        result = bundle(Path(argv[0]))
    except BundleError as exc:
        print(exc, file=sys.stderr)
        return 1
    Path(argv[1]).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
