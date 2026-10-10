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
    assert "uv run --frozen schemathesis run" in text
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
