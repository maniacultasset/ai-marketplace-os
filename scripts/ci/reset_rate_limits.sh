#!/usr/bin/env bash
# Empty the local limiter (CODE-PLAN S12 T14): every bucket in `rate_limit_counters` (logins,
# the per-IP 60/min and the per-tenant 120/min) goes, so each e2e script starts with the real
# limiter on and a full budget. `make e2e` and `bootstrap.sh` run it before every e2e script;
# the scripts under scripts/e2e/ never touch the counters themselves.
#
# Usage: scripts/ci/reset_rate_limits.sh [base_url]   (local only: http://127.0.0.1 or localhost)
# The compose project comes from .worktree.env, as for every make target.
set -euo pipefail
cd "$(dirname "$0")/../.."

worktree_exports="$(scripts/ci/worktree_env.sh exports .worktree.env)" || exit 1
eval "$worktree_exports"
base="${1:-http://127.0.0.1:${API_HOST_PORT:-8000}}"
python3 scripts/ci/local_url.py "$base" || exit 2

docker compose exec -T postgres psql -U postgres -d marketplace_os -c 'TRUNCATE rate_limit_counters' >/dev/null
echo "reset_rate_limits: OK"
