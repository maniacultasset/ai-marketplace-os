"""Read `pnpm pack --dry-run --json` on stdin; fail unless the tarball holds exactly the generated
types and `package.json` (S3 AC13). Stdlib only."""

from __future__ import annotations

import json
import sys

EXPECTED = frozenset({"package.json", "src/api.d.ts", "src/contracts.d.ts", "src/index.ts"})


def main() -> int:
    raw = sys.stdin.read()
    start = raw.find("{")
    if start < 0:
        print("ts-pack-check: no JSON from pnpm pack", file=sys.stderr)
        return 1
    packed = {item["path"] for item in json.loads(raw[start:])["files"]}
    if packed != EXPECTED:
        extra, missing = sorted(packed - EXPECTED), sorted(EXPECTED - packed)
        print(f"ts-pack-check: extra {extra}, missing {missing}", file=sys.stderr)
        return 1
    print("ts-pack-check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
