#!/usr/bin/env bash
# Smoke test for the local mock over a real socket (S3 AC12): start `python -m app_mock` on a free
# 127.0.0.1 port, expect fixture data from GET /api/v1/products and a 404 envelope from
# GET /api/v1/products/{product_id} with `Prefer: code=404`, then stop it.
set -euo pipefail
cd "$(dirname "$0")/../.."

port="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
log="$(mktemp)"
uv run --frozen python -m app_mock --host 127.0.0.1 --port "$port" >"$log" 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; rm -f "$log"' EXIT

base="http://127.0.0.1:${port}/api/v1"
for _ in $(seq 1 50); do
  if curl -fsS -o /dev/null "$base/products" 2>/dev/null; then break; fi
  if ! kill -0 "$pid" 2>/dev/null; then echo "mock-smoke: mock exited:"; cat "$log"; exit 1; fi
  sleep 0.2
done

body="$(curl -fsS "$base/products")"
python3 -c 'import json, sys; d = json.loads(sys.argv[1]); assert len(d["data"]) >= 1 and d["data"][0]["sku"], d' "$body" \
  || { echo "mock-smoke: GET /products has no fixture data"; exit 1; }

status="$(curl -sS -o "$log.404" -w '%{http_code}' -H 'Prefer: code=404' "$base/products/0b1c2d3e-0000-4000-8000-000000000101")"
[ "$status" = "404" ] || { echo "mock-smoke: Prefer: code=404 gave $status"; exit 1; }
python3 -c 'import json, sys; e = json.load(open(sys.argv[1]))["error"]; assert e["type"] == "NOT_FOUND" and e["status"] == 404, e' "$log.404" \
  || { echo "mock-smoke: 404 is not the error envelope"; rm -f "$log.404"; exit 1; }
rm -f "$log.404"
echo "mock-smoke: OK on 127.0.0.1:${port}"
