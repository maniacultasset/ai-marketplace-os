"""A Starlette app answering every operation in openapi.json with its declared example.

- The success example (2xx body, a 303 `Location`, or an empty 204) by default.
- `Prefer: code=<status>` answers that status's declared example, when the op declares it;
  otherwise a 422 envelope saying so (the FE cannot mock a status the contract never returns).
- An unknown path is a 404 envelope; a known path with another method is a 405 envelope.
Error bodies are the spec's own `ErrorEnvelope` examples, so they always match the contract.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

_PARAM: Final = re.compile(r"\{[^/{}]+\}")
_PREFER: Final = re.compile(r"(?:^|[;,\s])code=(\d{3})(?:$|[;,\s])")
METHODS: Final = ("get", "post", "put", "patch", "delete")


@dataclass(frozen=True)
class MockOperation:
    method: str
    template: str
    regex: re.Pattern[str]
    responses: dict[str, Any]


def _compile(template: str) -> re.Pattern[str]:
    parts = _PARAM.split(template)
    return re.compile("^" + "[^/]+".join(re.escape(p) for p in parts) + "$")


def _operations(spec: dict[str, Any]) -> list[MockOperation]:
    ops: list[MockOperation] = []
    for template, item in sorted(spec.get("paths", {}).items()):
        for method in METHODS:
            if method in item:
                ops.append(
                    MockOperation(
                        method.upper(), template, _compile(template), item[method]["responses"]
                    )
                )
    # Literal paths before templated ones, so /listings/validate never reads as /listings/{id}.
    ops.sort(key=lambda op: (op.template.count("{"), op.template))
    return ops


def _example(response: dict[str, Any]) -> Any:
    return response.get("content", {}).get("application/json", {}).get("example")


def _error_templates(spec: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """One declared envelope example per status, taken from the spec itself."""
    found: dict[int, dict[str, Any]] = {}
    for item in spec.get("paths", {}).values():
        for method in METHODS:
            for status, response in item.get(method, {}).get("responses", {}).items():
                example = _example(response)
                if status.isdigit() and int(status) >= 400 and isinstance(example, dict):
                    found.setdefault(int(status), example)
    return found


def _envelope(
    templates: dict[int, dict[str, Any]], status: int, code: str, message: str
) -> JSONResponse:
    base = templates.get(status) or templates[422]
    body = copy.deepcopy(base)
    error = body["error"]
    error.update({"type": code, "status": status, "message": message, "details": None})
    report = error["error_report"]
    report["error_code"] = code
    for item in report["errors"]:
        item.update({"error_code": code, "message": message})
    return JSONResponse(body, status_code=status)


def _success(op: MockOperation) -> Response:
    for status, response in sorted(op.responses.items()):
        if not status.isdigit() or not 200 <= int(status) < 400:
            continue
        code = int(status)
        if code == 303:
            location = response.get("headers", {}).get("Location", {}).get("example", "/")
            return Response(status_code=303, headers={"Location": location})
        example = _example(response)
        if example is None:
            return Response(status_code=code)
        return JSONResponse(example, status_code=code)
    return Response(status_code=204)


def create_mock_app(spec_path: Path) -> Starlette:
    spec: dict[str, Any] = json.loads(spec_path.read_text(encoding="utf-8"))
    ops = _operations(spec)
    templates = _error_templates(spec)

    async def handle(request: Request) -> Response:
        path = request.url.path
        matching = [op for op in ops if op.regex.match(path)]
        if not matching:
            return _envelope(templates, 404, "NOT_FOUND", "Not found.")
        op = next((o for o in matching if o.method == request.method), None)
        if op is None:
            response = _envelope(templates, 405, "METHOD_NOT_ALLOWED", "Method not allowed.")
            response.headers["Allow"] = ", ".join(sorted({o.method for o in matching}))
            return response
        prefer = _PREFER.search(request.headers.get("prefer", ""))
        if prefer is None:
            return _success(op)
        wanted = prefer.group(1)
        declared = op.responses.get(wanted)
        if declared is None:
            return _envelope(
                templates,
                422,
                "VALIDATION_FAILED",
                f"Prefer: code={wanted} is not declared for {op.method} {op.template}.",
            )
        example = _example(declared)
        if example is None:
            return Response(status_code=int(wanted))
        return JSONResponse(example, status_code=int(wanted))

    return Starlette(routes=[Route("/{path:path}", handle, methods=[m.upper() for m in METHODS])])
