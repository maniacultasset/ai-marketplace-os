"""S26 T20-T21 (AC 9, AC 3, DW3; PLAN D3-D5, D14): the v1 acceptance run
(scripts/e2e/acceptance.sh) and `make acceptance` (scripts/e2e/lib/acceptance_run.sh)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = "scripts/e2e/acceptance.sh"
RUNNER = "scripts/e2e/lib/acceptance_run.sh"

PART1 = (
    "setup",
    "login_owner",
    "login_operator",
    "login_reviewer",
    "connect_amazon",
    "import_csv",
    "brand_document_md",
    "images_upload",
)
PART2 = (
    "generate_stub",
    "validate_passed",
    "owner_self_approve_item_403",
    "operator_approve_permission_denied",
    "reviewer_approve_200",
    "preview",
    "publish",
    "retry_fields",
    "submissions_attempt2",
    "retry_succeeded",
    "remap_skip",
    "status_sync",
    "generate_flipkart_stub",
    "flipkart_publish_refused",
    "publish_replay",
    "analytics",
    "ask_question_skip",
    "no_canaries_in_logs",
)


def _text(repo_root: Path, name: str = SCRIPT) -> str:
    return (repo_root / name).read_text(encoding="utf-8")


def _legs(text: str) -> list[str]:
    return re.findall(r"^leg (\w+)$", text, re.MULTILINE)


def _leg_bodies(text: str) -> dict[str, str]:
    """Each leg's script text, from its `leg` line to the next one."""
    parts = re.split(r"^leg (\w+)$", text, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def test_brd_walkthrough_part1_legs_named(repo_root: Path) -> None:
    legs = _legs(_text(repo_root))
    assert legs[: len(PART1)] == list(PART1)


def test_brd_walkthrough(repo_root: Path) -> None:
    """Every leg of T20 + T21, in order; the maker-checker trio on one version; the Flipkart
    listing generated before its refused publish; no `operator_approve_403` leg."""
    text = _text(repo_root)
    assert "set -euo pipefail" in text
    legs = _legs(text)
    assert legs == [*PART1, *PART2]
    assert "operator_approve_403" not in legs
    order = legs.index
    assert (
        order("owner_self_approve_item_403")
        < order("operator_approve_permission_denied")
        < order("reviewer_approve_200")
    )
    assert order("generate_flipkart_stub") < order("flipkart_publish_refused")
    assert order("images_upload") < order("generate_stub")  # the S17b image gate at approve
    bodies = _leg_bodies(text)
    for leg in ("owner_self_approve_item_403", "operator_approve_permission_denied"):
        assert '"items\\": [$decision]}' in bodies[leg] or "[$decision]" in bodies[leg], leg
    assert "[$decision]" in bodies["reviewer_approve_200"]  # one listing_version_id, all three
    assert "MAKER_CHECKER_VIOLATION" in bodies["owner_self_approve_item_403"]
    assert "listing.approve_denied" in bodies["owner_self_approve_item_403"]
    assert "PERMISSION_DENIED" in bodies["operator_approve_permission_denied"]
    assert "expect 403" in bodies["operator_approve_permission_denied"]
    assert "FAKE-QUOTA" in bodies["publish"] and '"$style-XL"' in bodies["publish"].replace(
        "\\", ""
    )
    assert "retryable_condition" in bodies["submissions_attempt2"]
    assert "corrections_json" in bodies["submissions_attempt2"]
    assert "NOT_IMPLEMENTED" in bodies["flipkart_publish_refused"]
    assert 'http_status"] == 501' in bodies["flipkart_publish_refused"]
    assert "cmp -s" in bodies["publish_replay"]


def test_brd_walkthrough_setup_only_non_http(repo_root: Path) -> None:
    """D5: the staff CLI (tenant create, add-member, set-password, platform load) only in setup;
    nothing reads or writes the database directly anywhere."""
    text = _text(repo_root)
    bodies = _leg_bodies(text)
    setup = bodies["setup"]
    for command in (
        "platform load",
        "tenant create",
        "tenant add-member",
        "user set-password",
    ):
        assert re.search(rf"^cli \S+ {command}", setup, re.MULTILINE), command
    for leg, body in bodies.items():
        if leg != "setup":
            assert not re.search(r"^\s*cli ", body, re.MULTILINE), leg
            assert "MKOS_CLI" not in body, leg
    for banned in ("psql", "docker compose exec", "docker exec", "app.workers.oneshot"):
        assert banned not in text, banned


def test_acceptance_asserts_only_via_http(repo_root: Path) -> None:
    """Every state change and every assertion after setup goes through `call`, `multipart` or
    the fake marketplace's own HTTP endpoints; no Python reaches the app or the database."""
    text = _text(repo_root)
    assert "import app" not in text and "from app" not in text
    assert "psycopg" not in text and "sqlalchemy" not in text
    bodies = _leg_bodies(text)
    for leg, body in bodies.items():
        if leg in ("setup", "remap_skip", "ask_question_skip", "no_canaries_in_logs"):
            continue
        assert re.search(
            r"\b(call|multipart|wait_job|wait_listing|fake_control|login)\b", body
        ), leg


def test_acceptance_status_sync_via_refresh_status(repo_root: Path) -> None:
    bodies = _leg_bodies(_text(repo_root))
    sync = bodies["status_sync"]
    assert "/refresh-status" in sync and "expect 202" in sync
    assert '"buyable"' in sync and "fake_control" in sync
    assert 'b["marketplace_state"] == "live"' in sync and 'b["live_url"]' in sync
    assert "listing_status_sync" not in _text(repo_root)


def test_acceptance_named_skips_c2_t41(repo_root: Path) -> None:
    bodies = _leg_bodies(_text(repo_root))
    assert 'echo "acceptance: SKIP T41' in bodies["remap_skip"]
    assert 'echo "acceptance: SKIP C2' in bodies["ask_question_skip"]


def test_brd_walkthrough_local_urls_only(repo_root: Path) -> None:
    text = _text(repo_root)
    assert 'python3 scripts/ci/local_url.py "$base" || exit 2' in text
    assert 'python3 scripts/ci/local_url.py "$fake" || exit 2' in text
    for url in re.findall(r"https?://[^\s\"'$*|)]+", text):
        assert url.startswith(
            ("http://127.0.0.1", "http://localhost", "https://cdn.example.com")
        ), url


@pytest.mark.parametrize("url", ["http://10.1.2.3:8000", "https://127.0.0.1:8000", "http://x.test"])
def test_brd_walkthrough_refuses_non_local_url(repo_root: Path, tmp_path: Path, url: str) -> None:
    env = {"PATH": "/usr/bin:/bin", "MKOS_CLI": "false", "FAKE_URL": "http://127.0.0.1:9"}
    for base, fake in ((url, "http://127.0.0.1:9"), ("http://127.0.0.1:9", url)):
        result = subprocess.run(
            [str(repo_root / SCRIPT), base],
            capture_output=True,
            text=True,
            env={**env, "FAKE_URL": fake},
            timeout=60,
            check=False,
        )
        assert result.returncode == 2, result.stderr


def test_brd_walkthrough_missing_hooks_exit_2(repo_root: Path) -> None:
    for env in ({"FAKE_URL": "http://127.0.0.1:9"}, {"MKOS_CLI": "false"}):
        result = subprocess.run(
            [str(repo_root / SCRIPT), "http://127.0.0.1:9"],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", **env},
            timeout=60,
            check=False,
        )
        assert result.returncode == 2
        assert result.stderr.startswith("usage: FAKE_URL=<fake> MKOS_CLI=<cmd prefix>")


def test_brd_walkthrough_run_unique_keys(repo_root: Path) -> None:
    """A rerun on the same stack never collides: slug, emails, seller and every key carry $run."""
    text = _text(repo_root)
    assert 'run="$(date +%s)$RANDOM"' in text
    assert 'slug="acc-$run"' in text and 'seller="ACC$run"' in text
    keys = re.findall(r'"(acc-[^"\s]*)"', text)
    assert keys and all("$run" in k for k in keys), keys
    for email in re.findall(r'^\w+_email="([^"]+)"', text, re.MULTILINE):
        assert "$slug" in email or email == "staff@example.test", email
    assert "new_password" in text and "secrets.token_hex" in text  # per-run passwords


def test_brd_walkthrough_no_fixed_sleep(repo_root: Path) -> None:
    """Only bounded 1 s polls; never a fixed wait for a state."""
    text = _text(repo_root)
    sleeps = re.findall(r"\bsleep\s+(\S+)", text)
    assert sleeps and set(sleeps) == {"1"}, sleeps
    for match in re.finditer(r"\bsleep 1\b", text):
        before = text[: match.start()]
        loop = before.rfind("while :; do")
        assert loop != -1 and "deadline" in before[loop:], "a sleep outside a bounded poll"


def test_brd_walkthrough_canaries_from_every_secret(repo_root: Path) -> None:
    """AC 3 over the run: every password, access token and refresh token goes on the canary
    list, and the last leg greps the stack's logs for each."""
    text = _text(repo_root)
    assert 'json \'b["refresh_token"]\' >>"$work/canaries"' in text
    assert 'json \'b["access_token"]\' | tee -a "$work/canaries"' in text
    assert 'cat "$work/$who.password" >>"$work/canaries"' in text
    assert 'grep -F -r -q -f "$work/canaries" "$STACK_LOGS"' in text


def test_makefile_acceptance_target(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert ".PHONY: acceptance\nacceptance:\n\tscripts/e2e/lib/acceptance_run.sh" in makefile
    assert '[ "$$(basename "$$script")" != acceptance.sh ] || continue;' in makefile
    runner = _text(repo_root, RUNNER)
    assert "set -euo pipefail" in runner
    assert "scripts/e2e/lib/fresh_stack.sh up --profile acceptance" in runner
    assert "fresh_stack.sh down" in runner and "trap cleanup EXIT" in runner
    assert '[ "${FLIPKART_ENABLED:-false}" = false ]' in runner
    assert "env ALEMBIC_DATABASE_URL=$ALEMBIC_DATABASE_URL DATABASE_URL=$DATABASE_URL" in runner
    assert 'scripts/e2e/acceptance.sh "$base_url"' in runner
    assert ".worktree.env" not in runner.split("set -euo pipefail", 1)[1]
    workflows = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (repo_root / ".github/workflows").glob("*.yml")
        if p.name != "nightly.yml"
    )
    assert "make acceptance" not in workflows  # not a required check
