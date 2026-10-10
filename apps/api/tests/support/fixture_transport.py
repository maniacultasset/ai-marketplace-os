"""`fixture_transport`: an `httpx.MockTransport` that answers one marketplace fixture (S22 T14).

A fixture is `{provenance, request: {method, path, body}, response: {status, body}, ...}` under
`tests/fixtures/<marketplace>/`. The transport answers every request with the recorded response
after checking that the request has the recorded method, the recorded path (after the API base's
own path) and, for a body, the same listing keys per entry: the adapter's wire shape cannot drift
from the fixture silently. `provenance` is `believed` until a pair is recorded against the real
marketplace (`recorded`, with `recorded_at`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def load_fixture(marketplace: str, name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / marketplace / f"{name}.json").read_text("utf-8"))
    return data


def _keys(body: Any) -> list[list[str]]:
    entries = body.get("listings") if isinstance(body, dict) else None
    return [sorted(e) for e in entries] if isinstance(entries, list) else []


def fixture_transport(
    fixture: dict[str, Any], *, base_path: str = "/sellers"
) -> httpx.MockTransport:
    want = fixture["request"]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == want["method"], (request.method, want["method"])
        path = request.url.raw_path.decode("ascii")
        assert path == base_path + want["path"], (path, want["path"])
        if want["body"] is not None:
            got = json.loads(request.content)
            assert _keys(got) == _keys(want["body"]), (_keys(got), _keys(want["body"]))
        response = fixture["response"]
        return httpx.Response(response["status"], json=response["body"])

    transport = httpx.MockTransport(handler)
    transport.seen = seen  # type: ignore[attr-defined]
    return transport
