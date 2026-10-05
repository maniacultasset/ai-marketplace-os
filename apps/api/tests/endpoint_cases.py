"""The case-matrix guard's scanner (S3 AC9).

An implemented `/api/v1` route (its endpoint no longer carries the `@stub` marker) must ship one
test marked `@pytest.mark.endpoint("<op_id>", case="<case>")` for every case in `cases(op)`, and
the cases below need one marked test per variant. Markers are read from the source with `ast`,
so they must use literal strings; anything else fails the scan instead of being skipped.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

from app.api.v1.deps import bearer_required
from app.api.v1.surface import CASES, OPS, Operation, cases, is_stub
from fastapi.routing import iter_route_contexts

TESTS_ROOT: Final = Path(__file__).resolve().parent
REQUIRED_VARIANTS: Final[Mapping[str, frozenset[str]]] = {
    "invalid_token": frozenset({"expired", "bad_signature", "wrong_typ"}),
    "maker_checker": frozenset({"generator_approves", "editor_approves"}),
    "bad_state": frozenset({"forged", "expired", "replayed", "marketplace_mismatch"}),
}
Found = dict[str, dict[str, set[str | None]]]


class MarkerError(ValueError):
    """A malformed `endpoint` marker: non-literal arguments, an unknown op_id or case."""


def _is_endpoint_marker(func: ast.expr) -> bool:
    # pytest.mark.endpoint(...)
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "endpoint"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "mark"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "pytest"
    )


def _literal(node: ast.expr | None, where: str) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise MarkerError(f"{where}: endpoint marker arguments must be string literals")


def scan_source(source: str, where: str) -> list[tuple[str, str, str | None]]:
    marks: list[tuple[str, str, str | None]] = []
    for node in ast.walk(ast.parse(source, filename=where)):
        if not (isinstance(node, ast.Call) and _is_endpoint_marker(node.func)):
            continue
        loc = f"{where}:{node.lineno}"
        if len(node.args) != 1:
            raise MarkerError(f"{loc}: endpoint marker takes exactly one op_id")
        keywords = {k.arg: k.value for k in node.keywords}
        if None in keywords or set(keywords) - {"case", "variant"}:
            raise MarkerError(f"{loc}: endpoint marker takes only case= and variant=")
        op_id = _literal(node.args[0], loc)
        case = _literal(keywords.get("case"), loc)
        variant = _literal(keywords["variant"], loc) if "variant" in keywords else None
        if op_id not in OPS:
            raise MarkerError(f"{loc}: unknown op_id {op_id!r}")
        if case not in CASES:
            raise MarkerError(f"{loc}: unknown case {case!r}")
        marks.append((op_id, case, variant))
    return marks


def scan(root: Path = TESTS_ROOT) -> Found:
    found: Found = defaultdict(lambda: defaultdict(set))
    for path in sorted(root.rglob("*.py")):
        for op_id, case, variant in scan_source(path.read_text(encoding="utf-8"), str(path)):
            found[op_id][case].add(variant)
    return found


def missing_cases(op: Operation, found: Found) -> list[str]:
    have = found.get(op.op_id, {})
    missing: list[str] = []
    for case in sorted(cases(op)):
        variants = have.get(case, set())
        required = REQUIRED_VARIANTS.get(case)
        if required is None:
            if not variants:
                missing.append(case)
        else:
            missing.extend(f"{case}[{v}]" for v in sorted(required - variants))
    return missing


def _routes(app: Any) -> Iterable[Any]:
    for context in iter_route_contexts(app.routes):
        if getattr(context, "operation_id", None) is not None:
            yield context


def _calls(dependant: Any) -> Iterable[Any]:
    stack = [dependant]
    while stack:
        node = stack.pop()
        yield node.call
        stack.extend(node.dependencies)


def guard_failures(app: Any, found: Found) -> list[str]:
    """One message per implemented route that is short of its case matrix, still presence-only,
    or not in the registry at all."""
    failures: list[str] = []
    for context in _routes(app):
        op_id = context.operation_id
        if is_stub(context.endpoint):
            continue
        op = OPS.get(op_id)
        if op is None:
            failures.append(f"{op_id}: implemented route is not in the registry")
            continue
        missing = missing_cases(op, found)
        if missing:
            failures.append(f"{op_id}: missing endpoint cases: {', '.join(missing)}")
        if not op.public and bearer_required in _calls(context.dependant):
            failures.append(f"{op_id}: presence-only bearer_required")
    return failures
