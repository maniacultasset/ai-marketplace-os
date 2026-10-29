#!/usr/bin/env bash
# The monthly vulnerability scan of docs/security/VULNERABILITY-MANAGEMENT.md (Amazon DPP 2.7.1).
# Runs on a laptop with no Docker: every leg reads a lockfile, the git history or the registry.
#
#   scripts/security/monthly_scan.sh [--frontend <path to the web repo>] [--project <gcp project>]
#
# Legs, in order:
#   1. pip-audit of the locked Python packages (scripts/ci/run_pip_audit.py, the api-lint leg)
#   2. gitleaks over the whole git history of this repo (scripts/ci/gitleaks.sh)
#   3. npm audit of scripts/docgen (a build-time tool; never deployed)
#   4. pnpm audit --prod of the frontend, when --frontend is given
#   5. the live api image: Artifact Analysis findings when container scanning is on, else trivy
#      (remote image, no Docker) when it is installed; otherwise the leg is reported SKIPPED
# Each leg prints PASS, FINDINGS or SKIPPED; the exit code is 0 only when every leg passed.
# Read-only against Google Cloud: describe and list calls only.
set -uo pipefail
cd "$(dirname "$0")/../.."

FRONTEND=""
PROJECT="aimos-prod"
REGION="asia-south1"
SERVICE="aimos-api"
while [ $# -gt 0 ]; do
  case "$1" in
    --frontend) FRONTEND="${2:?--frontend needs a path}"; shift 2 ;;
    --project) PROJECT="${2:?--project needs an id}"; shift 2 ;;
    *) echo "usage: $0 [--frontend <path>] [--project <id>]" >&2; exit 2 ;;
  esac
done

STATUS=0
RESULTS=()
record() {
  RESULTS+=("$1: $2")
  [ "$2" = "PASS" ] || STATUS=1
}

echo "== monthly scan $(date -u +%Y-%m-%dT%H:%MZ) at $(git rev-parse --short=12 HEAD)"

echo "== 1. pip-audit"
if uv run --frozen python scripts/ci/run_pip_audit.py; then record pip-audit PASS; else record pip-audit FINDINGS; fi

echo "== 2. gitleaks (all history)"
if BASE_SHA=0 HEAD_SHA="$(git rev-parse HEAD)" scripts/ci/gitleaks.sh; then
  record gitleaks PASS
else
  record gitleaks FINDINGS
fi

echo "== 3. npm audit scripts/docgen"
if command -v npm >/dev/null; then
  if (cd scripts/docgen && npm audit --package-lock-only --audit-level=high); then
    record docgen-npm-audit PASS
  else
    record docgen-npm-audit FINDINGS
  fi
else
  record docgen-npm-audit SKIPPED
fi

echo "== 4. pnpm audit --prod (frontend)"
if [ -z "$FRONTEND" ]; then
  echo "no --frontend path given"
  record frontend-pnpm-audit SKIPPED
elif command -v pnpm >/dev/null; then
  if (cd "$FRONTEND" && pnpm audit --prod --audit-level=high); then
    record frontend-pnpm-audit PASS
  else
    record frontend-pnpm-audit FINDINGS
  fi
else
  record frontend-pnpm-audit SKIPPED
fi

echo "== 5. live api image"
image="$(gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" \
  --format='value(spec.template.spec.containers[0].image)' 2>/dev/null || true)"
if [ -z "$image" ]; then
  echo "cannot read the live image of $SERVICE in $PROJECT"
  record image-scan SKIPPED
elif gcloud services list --enabled --project="$PROJECT" --format='value(config.name)' 2>/dev/null |
  grep -qx containerscanning.googleapis.com; then
  echo "image $image (Artifact Analysis)"
  # The count of CRITICAL and HIGH occurrences, or "unknown" when the describe fails.
  findings="$(gcloud artifacts docker images describe "$image" --show-package-vulnerability \
    --format=json 2>/dev/null | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except ValueError:
    print("unknown")
    raise SystemExit
summary = (doc.get("package_vulnerability_summary") or {}).get("vulnerabilities") or {}
print(sum(len(summary.get(level) or []) for level in ("CRITICAL", "HIGH")))')"
  echo "critical + high: $findings"
  case "$findings" in
    0) record image-scan PASS ;;
    "" | unknown) record image-scan SKIPPED ;;
    *) record image-scan FINDINGS ;;
  esac
elif command -v trivy >/dev/null; then
  echo "image $image (trivy, remote)"
  if TRIVY_USERNAME=oauth2accesstoken TRIVY_PASSWORD="$(gcloud auth print-access-token)" \
    trivy image --image-src remote --severity CRITICAL,HIGH --exit-code 1 --quiet "$image"; then
    record image-scan PASS
  else
    record image-scan FINDINGS
  fi
else
  echo "no scanner: turn on container scanning (docs/security/VULNERABILITY-MANAGEMENT.md) or install trivy"
  record image-scan SKIPPED
fi

echo "== summary"
printf '%s\n' "${RESULTS[@]}"
exit "$STATUS"
