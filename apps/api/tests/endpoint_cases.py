"""The case-matrix guard's scanner (S3 AC9).

An implemented `/api/v1` route (its endpoint no longer carries the `@stub` marker) must ship one
test marked `@pytest.mark.endpoint("<op_id>", case="<case>")` for every case in `cases(op)`, and
the cases below need one marked test per variant. Markers are read from the source with `ast`,
so they must use literal strings; anything else fails the scan instead of being skipped.
"""

from __future__ import annotations

import ast
import itertools
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from app.api.v1.deps import bearer_required
from app.api.v1.surface import CASES, OPS, Operation, cases, is_stub
from fastapi.routing import iter_route_contexts
from starlette.routing import Host, Match, Mount

TESTS_ROOT: Final = Path(__file__).resolve().parent
API_V1: Final = "/api/v1"
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


@dataclass(frozen=True)
class V1Route:
    """One route that serves `/api/v1` URLs, with its full declared path (prefix and mounts
    applied)."""

    label: str
    """`<METHOD> <path>`; `WEBSOCKET` or `MOUNT` when the route has no HTTP methods."""
    path: str
    methods: frozenset[str]
    operation_id: str | None
    endpoint: Any
    context: Any


_PROBE_PARAM_VALUES: Final = (
    "v1",
    "api",
    "1",
    "00000000-0000-0000-0000-000000000000",
    "__guard_probe__",
)
_PARAM: Final = re.compile(r"{[^}]*}")
_PATH_TAIL_VALUES: Final = ("", "/", "/__guard_probe__", "/__guard_probe__/x")


def _under_v1(path: str) -> bool:
    return path == API_V1 or path.startswith(f"{API_V1}/")


def _fill(path_format: str) -> set[str]:
    """`path_format` with every combination of probe values for its parameters (one shared value
    past three parameters), so a templated prefix such as `/api/{ver}/...` yields `/api/v1/...`."""
    slots = _PARAM.findall(path_format)
    if not slots:
        return {path_format}
    pieces = _PARAM.split(path_format)
    combos: Iterable[tuple[str, ...]]
    if len(slots) <= 3:
        combos = itertools.product(_PROBE_PARAM_VALUES, repeat=len(slots))
    else:
        combos = ((v,) * len(slots) for v in _PROBE_PARAM_VALUES)
    out: set[str] = set()
    for combo in combos:
        out.add("".join(p + v for p, v in zip(pieces, (*combo, ""), strict=True)))
    return out


def _aligned(path_format: str) -> set[str]:
    """URLs `path_format` could serve that start with `/api/v1` (R3-1): each parameter met inside
    that prefix takes every slice of it that it could match, so a parameter glued to a literal
    (`/api/{x}1/...` -> `x=v`) or spanning a slash (`/{p:path}`) still lands on `/api/v1`. The
    parameters past the prefix take the `_fill` probe values; the router decides what matches."""
    tokens = re.split(r"({[^}]*})", path_format)
    out: set[str] = set()

    def step(i: int, pos: int, built: str) -> None:
        if pos == len(API_V1):
            out.update(built + rest for rest in _fill("".join(tokens[i:])))
            if i < len(tokens) and tokens[i].endswith(":path}"):
                out.update(built + tail for tail in _PATH_TAIL_VALUES)
            return
        if i == len(tokens):
            return
        token, want = tokens[i], API_V1[pos:]
        if not token.startswith("{"):
            if token.startswith(want):
                out.update(built + token + rest for rest in _fill("".join(tokens[i + 1 :])))
            elif want.startswith(token):
                step(i + 1, pos + len(token), built + token)
            return
        spans_slash = token.endswith(":path}")
        for end in range(pos + 1, len(API_V1) + 1):
            piece = API_V1[pos:end]
            if "/" in piece and not spans_slash:
                break
            step(i + 1, end, built + piece)

    step(0, 0, "")
    return out


def _probe_paths(app: Any) -> set[str]:
    """`/api/v1` URLs to dispatch through the app: the prefix, unknown paths below it, each
    registry op's path, and each route's full path (mount prefixes applied) with its parameters
    filled in or aligned on `/api/v1`."""
    probes = {API_V1, f"{API_V1}/", f"{API_V1}/__guard_probe__", f"{API_V1}/__guard_probe__/x"}
    for op in OPS.values():
        probes |= _fill(f"{API_V1}{op.path}")

    def walk(routes: Iterable[Any], prefix: str) -> None:
        for context in iter_route_contexts(list(routes)):
            served = _served(context)
            if isinstance(served, (Mount, Host)):
                path = prefix + _mount_path(served)
                probes.update(_fill(path) | _aligned(path) | _aligned(f"{path}/{{rest:path}}"))
                walk(getattr(served, "routes", None) or (), path)
                continue
            path_format = getattr(served, "path_format", None) or getattr(served, "path", "")
            full = f"{prefix}{path_format}"
            probes.update(_fill(full) | _aligned(full))

    walk(app.routes, "")
    return {p for p in probes if _under_v1(p)}


def _served(context: Any) -> Any:
    # A websocket route or mount inside an included router is served by its prefixed copy.
    prefixed = getattr(context, "starlette_route", None)
    if prefixed is not None:
        return prefixed
    return context.original_route if context._route_context is None else context


def _mount_path(route: Any) -> str:
    return str(getattr(route, "path", "") or "") if isinstance(route, Mount) else ""


def _probe_scopes(path: str) -> list[dict[str, Any]]:
    """One http and one websocket scope for `path`, as the top-level router would see them."""
    return [
        {
            "type": kind,
            "path": path,
            "root_path": "",
            "method": "GET",
            "headers": [],
            "query_string": b"",
            "path_params": {},
        }
        for kind in ("http", "websocket")
    ]


def v1_routes(app: Any) -> Iterable[V1Route]:
    """Every route that serves `/api/v1` URLs, picked by real dispatch and not by its declared
    path (R2-1, R3-1): each probe URL goes through `route.matches`, and a mount is entered only
    with the child scopes of the probes it FULL-matches, so a templated prefix (`/api/{ver}/...`),
    a catch-all (`/api/v1{rest:path}`), a templated or nested mount and a parameter glued to a
    literal are all in scope. Routes without an `operation_id` (`include_in_schema=False`) count
    too (R1-2). A mount the guard cannot look into is yielded whole when it serves `/api/v1` URLs;
    a `Host` is entered as is, since the probes carry no host header."""
    scopes = [s for p in sorted(_probe_paths(app)) for s in _probe_scopes(p)]

    def walk(routes: Iterable[Any], prefix: str, scopes: list[dict[str, Any]]) -> Iterable[V1Route]:
        for context in iter_route_contexts(list(routes)):
            served = _served(context)
            declared = f"{prefix}{getattr(served, 'path', '') or ''}"
            mounted = isinstance(served, (Mount, Host))
            subroutes = getattr(served, "routes", None) if mounted else None
            if isinstance(served, Host) and subroutes:
                yield from walk(subroutes, prefix, scopes)
                continue
            matched: list[dict[str, Any]] = []
            for scope in scopes:
                match, child = served.matches(scope)
                if match in (Match.FULL, Match.PARTIAL):
                    matched.append({**scope, **child})
            if isinstance(served, Mount) and subroutes and not _under_v1(declared):
                # Look inside with the stripped child scopes: only the sub-app routes that serve
                # /api/v1 URLs are in scope.
                yield from walk(subroutes, declared, matched)
                continue
            if not (_under_v1(declared) or matched):
                continue
            methods = frozenset(getattr(served, "methods", None) or ())
            kind = "MOUNT" if mounted else "WEBSOCKET"
            yield V1Route(
                label=f"{','.join(sorted(methods)) or kind} {declared or '/'}",
                path=declared,
                methods=methods,
                operation_id=getattr(served, "operation_id", None),
                endpoint=getattr(served, "endpoint", None),
                context=context,
            )

    yield from walk(app.routes, "", scopes)


def _registered(route: V1Route) -> Operation | None:
    """The registry op this route serves: same op_id, method and full path, or None."""
    op = OPS.get(route.operation_id or "")
    if op is None or route.methods != {op.method} or route.path != f"{API_V1}{op.path}":
        return None
    return op


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
    seen: set[str] = set()
    for v1_route in v1_routes(app):
        op = _registered(v1_route)
        if op is None:
            failures.append(f"{v1_route.label}: route is not in the registry")
            continue
        if op.op_id in seen:
            failures.append(f"{v1_route.label}: duplicate route for {op.op_id}")
            continue
        seen.add(op.op_id)
        context = v1_route.context
        if is_stub(v1_route.endpoint):
            continue
        op_id = op.op_id
        missing = missing_cases(op, found)
        if missing:
            failures.append(f"{op_id}: missing endpoint cases: {', '.join(missing)}")
        if not op.public and bearer_required in _calls(context.dependant):
            failures.append(f"{op_id}: presence-only bearer_required")
    return failures
