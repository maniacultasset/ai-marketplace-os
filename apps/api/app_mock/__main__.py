"""`python -m app_mock --port 4010 [--spec path]`: serve the mock on loopback only."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import uvicorn

from app_mock.server import create_mock_app

LOOPBACK: Final = frozenset({"127.0.0.1", "::1"})
DEFAULT_SPEC: Final = (
    Path(__file__).resolve().parents[3] / "packages/schemas/jsonschema/openapi.json"
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app_mock")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4010)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.host not in LOOPBACK:
        print(
            f"app_mock: refusing host {args.host!r}: loopback only (127.0.0.1 or ::1)",
            file=sys.stderr,
        )
        return 2
    if not 1 <= args.port <= 65535:
        print("app_mock: --port must be 1..65535", file=sys.stderr)
        return 2
    uvicorn.run(create_mock_app(args.spec), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
