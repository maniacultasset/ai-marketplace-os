#!/bin/sh
# Rebuild the team .docx files from the .md sources. The .md is the source of truth (AGENTS.md §8).
# First run: (cd scripts/docgen && npm install). Needs Google Chrome for diagram rendering.
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HERE="$ROOT/scripts/docgen"
OUT="$ROOT/deliverables"
CACHE="$HERE/.cache"                    # build cache stays with the tool; deliverables/ holds only .docx
[ -d "$HERE/node_modules/docx" ] || { echo "run: (cd scripts/docgen && npm install)"; exit 1; }
mkdir -p "$CACHE"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
PPTR="$CACHE/pptr.json"
# Offline by design (AGENTS.md §1): every hostname fails to resolve, so Chrome can't send anything out.
# Puppeteer gives Chrome a throwaway profile, never the user's own.
printf '{"executablePath":"%s","args":["--no-sandbox","--host-resolver-rules=MAP * ~NOTFOUND","--disable-background-networking","--disable-component-update","--disable-sync","--disable-domain-reliability","--no-first-run","--no-default-browser-check"]}\n' "$CHROME" > "$PPTR"
VERSION="${VERSION:-Draft $(date +%Y-%m-%d)}"
run() { DOCGEN_CACHE="$CACHE/img" DOCGEN_DEPS="$HERE" MMDC="$HERE/node_modules/.bin/mmdc" PPTR_CONFIG="$PPTR" node "$HERE/md_to_docx.mjs" "$@"; }

# 0. "Decisions needed" page, generated from TEAM-QUESTIONS.md, the ADR ledger and DATABASE.md §11.1.
ADRS=$(python3 "$HERE/decisions.py" "$ROOT" "$CACHE/decisions.md")
FLAGS='\[(?:Proposed|Pending)[^\]]*\]|\bProposed\b|\bT[0-9]{1,3}\b|\b[Pp]rovisional\b'
[ -n "$ADRS" ] && FLAGS="$FLAGS|$ADRS"
LEGEND="Amber = an open decision: a [Proposed] item, a proposed ADR, a team question (T-number) or anything provisional. See 'Decisions needed' first."

# 1. Architecture: BACKEND-ARCHITECTURE.md with chapter 7 generated from DATABASE.md.
python3 "$HERE/db_chapter.py" "$ROOT/docs/architecture/BACKEND-ARCHITECTURE.md" \
  "$ROOT/docs/architecture/DATABASE.md" "$CACHE/arch-word.md"
run "$CACHE/decisions.md" "$OUT/AI Marketplace OS — Backend & DB Architecture.docx" --extra "$CACHE/arch-word.md" \
  --title "AI Marketplace OS" --subtitle "Backend & Database Architecture" --version "$VERSION" \
  --drop "Review notes" --flag "$FLAGS" --legend "$LEGEND"

# 2. Reference: the full, column-level database design.
run "$CACHE/decisions.md" "$OUT/AI Marketplace OS — Database Design (reference).docx" --extra "$ROOT/docs/architecture/DATABASE.md" \
  --title "AI Marketplace OS" --subtitle "Database Design (reference)" --version "$VERSION" \
  --drop "Review notes" --flag "$FLAGS" --legend "$LEGEND"
