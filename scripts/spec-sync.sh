#!/bin/sh
# Check the client spec site (source of truth) for changes against docs/source/.
# Read-only by default: downloads to a temp dir, reports what changed, and overwrites nothing.
# Pass --apply to replace the snapshot. Do that only after the user has seen the diff.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; SRC="$ROOT/docs/source"; BASE="https://system-arch.vercel.app"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
curl -fsS -m 60 -o "$TMP/llms-full.txt" "$BASE/llms-full.txt"
curl -fsS -m 60 -o "$TMP/content.json" "$BASE/content.json"
curl -fsS -m 60 -o "$TMP/llms.txt" "$BASE/llms.txt"
old_upd=$(python3 -c "import json;print(json.load(open('$SRC/system-arch-content.json'))['updatedAt'])")
new_upd=$(python3 -c "import json;print(json.load(open('$TMP/content.json'))['updatedAt'])")
if cmp -s "$TMP/llms-full.txt" "$SRC/system-arch-llms-full.txt" && cmp -s "$TMP/content.json" "$SRC/system-arch-content.json"; then
  echo "Spec unchanged (updatedAt $old_upd)."; exit 0
fi
echo "SPEC CHANGED: updatedAt $old_upd -> $new_upd"
python3 - "$SRC/system-arch-content.json" "$TMP/content.json" <<'PY'
import json,sys
a={p['id']:p for p in json.load(open(sys.argv[1]))['pages']}; b={p['id']:p for p in json.load(open(sys.argv[2]))['pages']}
for i in sorted(set(b)-set(a)): print('  + added page:', b[i]['num'], b[i]['title'])
for i in sorted(set(a)-set(b)): print('  - removed page:', a[i]['num'], a[i]['title'])
for i in sorted(set(a)&set(b)):
    if a[i].get('markdown')!=b[i].get('markdown') or a[i].get('scope')!=b[i].get('scope'):
        print('  ~ changed page:', b[i]['num'], b[i]['title'], '(scope %s -> %s)'%(a[i].get('scope'),b[i].get('scope')) if a[i].get('scope')!=b[i].get('scope') else '')
PY
if [ "${1:-}" = "--apply" ]; then
  cp "$TMP/llms-full.txt" "$SRC/system-arch-llms-full.txt"; cp "$TMP/content.json" "$SRC/system-arch-content.json"; cp "$TMP/llms.txt" "$SRC/system-arch-llms.txt"
  echo "Snapshot updated. Re-run the traceability check for the changed pages."
else
  echo "Nothing overwritten. Review the changes with the user, then re-run with --apply."
fi
