"""S26 T15 (AC 4): `make fuzz`, the live Schemathesis run (scripts/fuzz/run.sh)."""

from __future__ import annotations

import re
from pathlib import Path

SCRIPT = "scripts/fuzz/run.sh"


def _text(repo_root: Path) -> str:
    return (repo_root / SCRIPT).read_text(encoding="utf-8")


def test_fuzz_script_all_checks_stateful_seed_printed(repo_root: Path) -> None:
    text = _text(repo_root)
    assert "set -euo pipefail" in text
    assert "--checks all" in text
    # only the two schema-precision checks are excluded (unsupported_method runs since S26 fix
    # round 1); a 5xx always fails the run
    excluded = re.search(r"--exclude-checks (\S+)", text)
    assert excluded is not None
    assert set(excluded.group(1).split(",")) == {
        "positive_data_acceptance",
        "negative_data_rejection",
    }
    assert "not_a_server_error" not in excluded.group(1)
    assert "--phases examples,coverage,fuzzing,stateful" in text
    assert '--seed "$seed"' in text and '--max-examples "$max_examples"' in text
    assert 'echo "fuzz: seed=$seed' in text
    assert "uv run --frozen schemathesis --config-file scripts/fuzz/schemathesis.toml" in text
    assert 'run "$base_url/openapi.json"' in text
    assert 'exit "$status"' in text and "|| status=1" in text


def test_fuzz_script_local_urls_only(repo_root: Path) -> None:
    text = _text(repo_root)
    assert "http://127.0.0.1:*|http://localhost:*) ;;" in text
    assert "exit 2" in text
    assert "scripts/e2e/lib/fresh_stack.sh up --profile acceptance" in text
    assert "fresh_stack.sh down" in text and "trap cleanup EXIT" in text
    for url in re.findall(r"https?://[^\s\"'$*|]+", text):
        assert url.startswith(("http://127.0.0.1", "http://localhost")), url


def test_fuzz_script_raised_limits_only_in_process_env(repo_root: Path) -> None:
    """The raised limits reach only the fresh stack's API process (FRESH_ENV_FILE); no
    committed default changes."""
    text = _text(repo_root)
    assert 'FRESH_ENV_FILE="$limits"' in text
    for key in (
        "RATE_LIMIT_PER_TENANT_PER_MIN",
        "RATE_LIMIT_PER_IP_PER_MIN",
        "AUTH_LOGIN_RATE_LIMIT_PER_MIN",
    ):
        assert f"{key}=100000" in text
        assert f"export {key}" not in text
    # S26b: Flipkart on in that process env only, as `make dev` runs it (S22 C2)
    assert "FLIPKART_ENABLED=true" in text and "export FLIPKART_ENABLED" not in text
    example = (repo_root / ".env.example").read_text(encoding="utf-8")
    assert "RATE_LIMIT_PER_TENANT_PER_MIN=120" in example
    assert "RATE_LIMIT_PER_IP_PER_MIN=60" in example
    assert "AUTH_LOGIN_RATE_LIMIT_PER_MIN=5" in example
    from app.core.config import Settings

    fields = Settings.model_fields
    assert fields["rate_limit_per_tenant_per_min"].default == 120
    assert fields["rate_limit_per_ip_per_min"].default == 60
    assert fields["auth_login_rate_limit_per_min"].default == 5


def test_fuzz_script_runs_every_role(repo_root: Path) -> None:
    text = _text(repo_root)
    roles = re.search(r'^ROLES="([^"]+)"', text, re.MULTILINE)
    assert roles is not None
    order = roles.group(1).split()
    assert sorted(order) == ["admin", "operator", "owner", "reviewer"]
    assert order.index("admin") > order.index("reviewer") and order[-1] == "owner"
    assert 'if [ "$code" = 401 ]; then token="$(login "$role")"; fi' in text


def test_makefile_fuzz_target(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert (
        ".PHONY: fuzz\nfuzz:\n\tscripts/fuzz/run.sh --max-examples $(FUZZ_MAX_EXAMPLES)" in makefile
    )
    assert "FUZZ_MAX_EXAMPLES ?= 50" in makefile
    workflows = "\n".join(
        p.read_text(encoding="utf-8") for p in (repo_root / ".github/workflows").glob("*.yml")
    )
    assert "make fuzz" not in workflows  # not a required check


MFA_ROUTES = (
    "/auth/login/mfa",
    "/auth/login/mfa/enroll",
    "/auth/login/mfa/confirm",
    "/auth/password/expired",
    "/auth/mfa/totp/enroll",
    "/auth/mfa/totp/confirm",
    "/auth/mfa/recovery-codes",
    "/auth/mfa/step-up",
)


def test_fuzz_config_accepts_only_the_mfa_off_501(repo_root: Path) -> None:
    """G4 (10 Nov nightly): with MFA_ENFORCEMENT=off the eight MFA routes answer their documented
    501 NOT_IMPLEMENTED; the config accepts that 501 there and nowhere else, never a 500."""
    from app.api.v1.surface import OPS
    from schemathesis.config import SchemathesisConfig

    path = repo_root / "scripts/fuzz/schemathesis.toml"
    config = SchemathesisConfig.from_path(str(path))
    (entry,) = config.projects.default.operations.operations
    assert entry.checks.not_a_server_error.expected_statuses == ["2xx", "3xx", "4xx", "501"]
    regex = re.search(r"include-path-regex = '([^']+)'", path.read_text(encoding="utf-8"))
    assert regex is not None
    served = {f"/api/v1{op.path}" for op in OPS.values()}
    matched = {p for p in served if re.fullmatch(regex.group(1), p)}
    assert matched == {f"/api/v1{route}" for route in MFA_ROUTES}


def test_fuzz_script_google_on_against_the_stack_fake(repo_root: Path) -> None:
    """G4: Google sign-in on in the API's process env only, against this stack's fake Google
    (fresh_stack.sh fills @FAKE_URL@ and @BASE_URL@); the committed default stays off."""
    text = _text(repo_root)
    for line in (
        "GOOGLE_OAUTH_ENABLED=true",
        "GOOGLE_AUTHORIZE_URL=@FAKE_URL@/google/authorize",
        "GOOGLE_TOKEN_URL=@FAKE_URL@/google/token",
        "GOOGLE_JWKS_URL=@FAKE_URL@/google/certs",
        "GOOGLE_ISSUER=@FAKE_URL@/google",
        "DASHBOARD_BASE_URL=@BASE_URL@",
        "AUTH_GOOGLE_RATE_LIMIT_PER_MIN=100000",
    ):
        assert f"\n{line}\n" in text, line
    assert "MFA_ENFORCEMENT" not in text.split("<<'LIMITS'")[1].split("LIMITS")[0]
    stack = (repo_root / "scripts/e2e/lib/fresh_stack.sh").read_text(encoding="utf-8")
    assert '-e "s#@FAKE_URL@#$fake#g" -e "s#@BASE_URL@#$base#g"' in stack
    # the fake gets the same lines, so its Google client and redirect URI match the API's
    assert 'start fake "${proc_env[@]}" uv run --frozen python' in stack
    assert "\nGOOGLE_OAUTH_ENABLED=false\n" in (repo_root / ".env.example").read_text(
        encoding="utf-8"
    )
