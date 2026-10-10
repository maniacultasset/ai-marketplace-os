"""S26 T22 (AC 10): every v1 flow has an HTTP-only e2e script (scripts/e2e/*.sh), the scripts'
only database reads are allowlisted read-only `db_*` steps, they stay inside the limiter budget,
and `make e2e-fresh` runs them on a fresh stack. The flow map lives here (no data file)."""

from __future__ import annotations

import re
from pathlib import Path

E2E = "scripts/e2e"
# flow -> the scripts that carry it
FLOWS: dict[str, tuple[str, ...]] = {
    "auth": ("auth_session.sh",),
    "switch": ("auth_session.sh",),
    "members": ("members.sh",),
    "products": ("walking_skeleton.sh",),
    "import": ("import_flow.sh",),
    "brand": ("brand_onboarding.sh",),
    "images": ("image_pipeline.sh",),
    "generate": ("generate.sh",),
    "edit": ("listing_edit.sh",),
    "review": ("review_approve.sh",),
    "connect": ("marketplace_connect.sh", "flipkart_connect_preview.sh"),
    "preview": ("marketplace_preview.sh",),
    "publish": ("publish_flow.sh", "publish_recovery.sh"),
    "analytics": ("analytics.sh",),
    "rules": ("rules.sh",),
    "jobs": ("jobs.sh",),
}
# the steps that may read the database (`psql` through the compose postgres), each with its reason;
# every one is read-only (no write SQL), names as built
DB_ALLOWLIST: dict[tuple[str, str], str] = {
    ("walking_skeleton.sh", "db_row_under_acme"): "S12: the product row sits under acme's tenant",
    ("admin.sh", "db_read_seeded_secrets_then_grep"): "S14b: reads hashes/ciphertext, greps pages",
    ("marketplace_connect.sh", "db_ciphertext_no_canary"): "S15: token_enc holds no canary bytes",
    ("marketplace_connect.sh", "db_one_reconnect_task"): "S15: one open reconnect task",
    ("marketplace_connect.sh", "reconnect"): "S15: the reconnect task is applied (read-only)",
    ("marketplace_connect.sh", "db_revoked_shredded"): "S15: a revoked account holds no token",
    ("brand_onboarding.sh", "db_profile_chunks_present"): "S16: the profile's chunks are active",
    ("brand_onboarding.sh", "db_doc_chunks_inactive"): "S16: a deleted document's chunks",
    ("generate.sh", "db_version_and_runs"): "S18: one version, rules runs, agent runs",
    ("generate.sh", "db_failed_runs"): "S18: a failed generation has no version",
    ("regenerate.sh", "db_maker_is_reviewer"): "S18: the regeneration's maker",
    ("marketplace_preview.sh", "db_error_map_count_same"): "S20: the preview writes no error map",
}
# not a flow script: the onboarding clone harness (its own stack) and the acceptance run (its own
# meta test, test_brd_walkthrough.py; `make e2e` skips both)
NOT_FLOWS = frozenset({"onboarding.sh", "acceptance.sh"})
DB_CALL = re.compile(r"psql|docker compose exec|docker exec")
WRITE_SQL = re.compile(
    r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|TRUNCATE|DROP\s|ALTER\s|CREATE\s|GRANT\s)",
    re.IGNORECASE,
)
LOGIN_BUDGET = 5
REQUESTS_PER_MIN_BUDGET = 100
POLL_REQUESTS_PER_MIN = 60  # one poller at most, 1 s apart


def _scripts(repo_root: Path) -> dict[str, str]:
    return {
        p.name: p.read_text(encoding="utf-8")
        for p in sorted((repo_root / E2E).glob("*.sh"))
        if p.name not in NOT_FLOWS
    }


def _split(text: str) -> tuple[str, dict[str, str]]:
    """(the text before the first step: helpers and setup, {step: its text})."""
    parts = re.split(r"^step (\w+)$", text, flags=re.MULTILINE)
    steps: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        steps[parts[i]] = steps.get(parts[i], "") + parts[i + 1]
    return parts[0], steps


def _functions(prefix: str) -> dict[str, str]:
    """name -> body of each `name() {` ... `}` helper defined before the first step."""
    found: dict[str, str] = {}
    for match in re.finditer(r"^(\w+)\(\) \{\n(.*?)^\}$", prefix, re.MULTILINE | re.DOTALL):
        found[match.group(1)] = match.group(2)
    return found


def _code(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_every_flow_has_e2e_script(repo_root: Path) -> None:
    scripts = _scripts(repo_root)
    for flow, names in FLOWS.items():
        for name in names:
            assert name in scripts, f"{flow}: {name} missing"
            assert "set -euo pipefail" in scripts[name], name
    # the new S26 scripts cover their flows' named steps
    _, auth = _split(scripts["auth_session.sh"])
    for step in (
        "login_200",
        "me_200",
        "refresh_rotates_200",
        "refresh_reuse_401",
        "logout_204",
        "refresh_after_logout_401",
        "switch_tenant_200",
        "me_after_switch",
        "old_access_still_first_tenant",
        "old_refresh_rotates_own_family",
    ):
        assert step in auth, step
    _, members = _split(scripts["members.sh"])
    for step in (
        "list_members_200",
        "patch_operator_to_reviewer_200",
        "member_patch_403",
        "deactivate_200",
        "deactivated_refresh_401",
        "deactivated_login_401",
        "reactivate_200",
        "reactivated_login_200",
        "admin_patch_owner_403",
        "restore_operator_role_200",
    ):
        assert step in members, step
    _, jobs = _split(scripts["jobs.sh"])
    for step in ("poll_queued", "cancel_200", "cancel_again_409", "bharat_get_404"):
        assert step in jobs, step


def test_generate_flow_covers_failed_terminal(repo_root: Path) -> None:
    """S18's `stub:failed` legs, names as built (`job_result_no_version` is the planned
    `get_listing_no_current_version`)."""
    _, steps = _split(_scripts(repo_root)["generate.sh"])
    for step in (
        "generate_stub_failed_202",
        "poll_job_failed",
        "status_failed_generation_failed",
        "job_result_no_version",
    ):
        assert step in steps, step
    assert "stub:failed" in steps["generate_stub_failed_202"]


def test_e2e_scripts_http_only(repo_root: Path) -> None:
    """Every database call is in an allowlisted step, or in a helper only those steps call; no
    allowlisted read writes; no script truncates (the limiter reset is `make e2e`'s pre-step)."""
    for name, text in _scripts(repo_root).items():
        assert "TRUNCATE" not in text, name
        prefix, steps = _split(_code(text))
        helpers = {n: b for n, b in _functions(prefix).items() if DB_CALL.search(b)}
        outside = re.sub(r"^(\w+)\(\) \{\n.*?^\}$", "", prefix, flags=re.M | re.S)
        assert not DB_CALL.search(outside), f"{name}: a database call outside a helper or step"
        for helper, body in helpers.items():
            assert not WRITE_SQL.search(body), f"{name}: {helper} writes"
        for step, body in steps.items():
            reads = bool(DB_CALL.search(body)) or any(re.search(rf"\b{h}\b", body) for h in helpers)
            if reads:
                assert (name, step) in DB_ALLOWLIST, f"{name}: step {step} reads the database"
                assert not WRITE_SQL.search(body), f"{name}: {step} writes"


def test_e2e_allowlist_entries_exist(repo_root: Path) -> None:
    scripts = _scripts(repo_root)
    for (name, step), reason in DB_ALLOWLIST.items():
        assert reason, (name, step)
        assert name in scripts, name
        assert re.search(rf"^step {step}$", scripts[name], re.MULTILINE), (name, step)
        prefix, steps = _split(_code(scripts[name]))
        helpers = [n for n, b in _functions(prefix).items() if DB_CALL.search(b)]
        assert DB_CALL.search(steps[step]) or any(
            re.search(rf"\b{h}\b", steps[step]) for h in helpers
        ), f"{name}: {step} is allowlisted but reads nothing"


def test_e2e_scripts_within_rate_budget(repo_root: Path) -> None:
    """Static: at most 5 login call sites per email, every poll at least 1 s apart, and the call
    sites plus one 1 s poller stay within 100 requests a minute (the tenant limit is 120)."""
    for name, text in _scripts(repo_root).items():
        code = _code(text)
        logins: dict[str, int] = {}
        for match in re.finditer(r"\blogin \"?([^\s\"]+)", code):
            if match.group(1) in ("{", "EMAIL"):
                continue
            logins[match.group(1)] = logins.get(match.group(1), 0) + 1
        assert all(n <= LOGIN_BUDGET for n in logins.values()), (name, logins)
        for value in re.findall(r"\bsleep\s+(\S+)", code):
            assert float(value) >= 1, (name, value)
        calls = len(re.findall(r"^\s*(?:call|upload|multipart)\b|curl ", code, re.MULTILINE))
        polls = POLL_REQUESTS_PER_MIN if re.search(r"\bsleep\s+1\b", code) else 0
        assert calls + polls <= REQUESTS_PER_MIN_BUDGET, (name, calls, polls)


def test_make_e2e_resets_limits_before_each_script(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    loop = makefile[makefile.index("for script in $(E2E_DIR)/*.sh; do") :]
    loop = loop[: loop.index("done")]
    reset = loop.index('$(RESET_LIMITS) "$$base" || exit $$?;')
    run = loop.index('"$$script" "$$base" || exit $$?;')
    assert reset < run
    assert "RESET_LIMITS ?= scripts/ci/reset_rate_limits.sh" in makefile
    reset_script = (repo_root / "scripts/ci/reset_rate_limits.sh").read_text(encoding="utf-8")
    assert 'python3 scripts/ci/local_url.py "$base" || exit 2' in reset_script  # local only


def test_e2e_scripts_local_urls_only(repo_root: Path) -> None:
    for name, text in _scripts(repo_root).items():
        assert re.search(r'python3 scripts/ci/local_url\.py "\$base" \|\| exit 2', text), name
        for url in re.findall(r"https?://[^\s\"'$*|)\\]+", _code(text)):
            assert url.startswith(
                ("http://127.0.0.1", "http://localhost", "https://cdn.example.com")
            ), (name, url)


def test_new_scripts_never_touch_seed_users_by_cli(repo_root: Path) -> None:
    """auth_session.sh and members.sh make run-unique tenants and users; the seed stays as is."""
    scripts = _scripts(repo_root)
    for name in ("auth_session.sh", "members.sh"):
        text = scripts[name]
        prefix, _ = _split(text)
        assert 'run="$(date +%s)$RANDOM"' in prefix
        cli = "\n".join(line for line in text.splitlines() if line.startswith("cli "))
        assert "@acme.test" not in cli and "@bharat.test" not in cli, name
        assert '--slug "$' in cli, name


def test_make_e2e_fresh_uses_e2e_profile_and_overrides(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert ".PHONY: e2e-fresh\ne2e-fresh:\n\tscripts/e2e/lib/e2e_fresh.sh" in makefile
    text = (repo_root / "scripts/e2e/lib/e2e_fresh.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert "scripts/e2e/lib/fresh_stack.sh up --profile e2e" in text
    assert "fresh_stack.sh down" in text and "trap cleanup EXIT" in text
    assert 'PASSES="${E2E_FRESH_PASSES:-2}"' in text
    for key in (
        "COMPOSE_PROJECT_NAME",
        "PG_HOST_PORT",
        "MINIO_API_HOST_PORT",
        "MINIO_CONSOLE_HOST_PORT",
        "API_HOST_PORT",
        "FAKE_MARKETPLACE_HOST_PORT",
        "FLAG_OFF_API_HOST_PORT",
    ):
        assert key in text.split('KEYS="', 1)[1].split('"', 1)[0], key
    hooks = text.split('HOOKS="', 1)[1].split('"', 1)[0].split()
    assert hooks == ["WORKER_START", "WORKER_STOP", "TEMPLATES_LOAD", "ERRORS_LOAD", "MKOS_CLI"]
    assert 'overrides+=("E2E_$hook=${value//\\$/\\$\\$}")' in text
    assert 'make --no-print-directory "${overrides[@]}" e2e' in text
    assert 'export FRESH_STACK_WORKTREE_ENV="$stack/worktree.env"' in text
    assert "mkos-fresh-*) ;;" in text
    assert "WORKER_START=" not in text.replace("E2E_$hook", "")  # no worker of its own
    worktree = (repo_root / "scripts/ci/worktree_env.sh").read_text(encoding="utf-8")
    assert 'exports) exports_file "${FRESH_STACK_WORKTREE_ENV:-${2:-.worktree.env}}" ;;' in worktree
    for name in ("Makefile",):
        assert "FRESH_STACK_WORKTREE_ENV" not in (repo_root / name).read_text(encoding="utf-8")
    workflows = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (repo_root / ".github/workflows").glob("*.yml")
        if p.name != "nightly.yml"
    )
    assert "e2e-fresh" not in workflows  # not a required check
