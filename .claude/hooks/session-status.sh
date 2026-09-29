#!/bin/sh
# SessionStart hook: inject a compact project status from docs/STATE.md into Claude's context.
# Local only (no network). Prints nothing if STATE.md is missing.
f="${CLAUDE_PROJECT_DIR:-.}/docs/STATE.md"
[ -f "$f" ] || exit 0
echo "AI Marketplace OS — status from docs/STATE.md (full protocol: AGENTS.md §7):"
grep -E '^(_Last updated|\*\*(Active|Branch|Git repo|Hosting ADR|Tech Lead):)' "$f"
echo
awk '/^## Next actions/{p=1; print; next} p && /^## /{exit} p' "$f" | head -28
echo
echo "Tip: /drive continues the team loop (see .claude/skills/drive/SKILL.md)."
