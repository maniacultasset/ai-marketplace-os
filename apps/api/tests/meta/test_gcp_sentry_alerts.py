"""Ops readiness: Sentry issue alert rules as code (scripts/deploy/gcp/sentry_rules.py) and
`deploy.sh <env> sentry-alerts`. Offline: the helper's HTTP goes through a fake transport, and the
toolkit runs against the fake gcloud of test_gcp_edge and a fake helper interpreter."""

from __future__ import annotations

import io
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from tests.meta.test_gcp_edge import (
    _fake,
    gcloud_lines,
    run_dry,
    run_live,
    set_env,
    toolkit,
)

__all__ = ["toolkit"]
DEPLOY = "scripts/deploy/gcp"
TOKEN = "sntrys_" + "t" * 40
BASE = "https://de.sentry.io/api/0/projects/kuinn/aimos-api/rules/"
ARGS = [
    "--org",
    "kuinn",
    "--project",
    "aimos-api",
    "--api-url",
    "https://de.sentry.io",
    "--environment",
    "production",
]


@pytest.fixture(scope="module")
def sr(script: Callable[[str], ModuleType]) -> ModuleType:
    return script(f"{DEPLOY}/sentry_rules.py")


class FakeSentry:
    """The rules endpoint: GET lists `rules`, POST appends, PUT replaces by id."""

    def __init__(self, rules: list[dict[str, Any]] | None = None, fail: int | None = None) -> None:
        self.rules = rules or []
        self.calls: list[tuple[str, str, str]] = []
        self.fail = fail

    def __call__(self, method: str, url: str, token: str, body: bytes | None) -> tuple[int, bytes]:
        self.calls.append((method, url, token))
        if self.fail and method != "GET":
            return self.fail, b'{"detail": "nope"}'
        if method == "GET":
            return 200, json.dumps(self.rules).encode()
        doc = json.loads(body or b"{}")
        if method == "POST":
            doc["id"] = str(len(self.rules) + 1)
            self.rules.append(doc)
            return 201, json.dumps(doc).encode()
        rule_id = url.rstrip("/").rsplit("/", 1)[1]
        for i, rule in enumerate(self.rules):
            if rule["id"] == rule_id:
                self.rules[i] = {**doc, "id": rule_id}
        return 200, json.dumps(doc).encode()


def run_main(sr: ModuleType, fake: FakeSentry, *extra: str, token: str = TOKEN) -> int:
    return int(
        sr.main([*ARGS, *extra, "--token-stdin"], stdin=io.StringIO(token + "\n"), transport=fake)
    )


# ---- the helper ----


def test_rules_cover_new_regression_spike_and_ops_repeats(sr: ModuleType) -> None:
    rules = sr.rules("production")
    names = [r["name"] for r in rules]
    assert names == [
        "kuinn: new issue",
        "kuinn: regression",
        "kuinn: issue spike",
        "kuinn: ops alert repeats",
    ]
    for rule in rules:
        assert rule["environment"] == "production"
        assert rule["actions"] == [
            {
                "id": "sentry.mail.actions.NotifyEmailAction",
                "targetType": "IssueOwners",
                "fallthroughType": "ActiveMembers",
            }
        ]
    ops = rules[3]
    kinds = {f["value"] for f in ops["filters"]}
    # every kind is a real alert kind of app/core/alerts.py, the tag emit_alert sets
    from app.core.alerts import ALERT_KINDS

    assert kinds <= ALERT_KINDS and {"llm_provider_refused", "llm_credit"} <= kinds
    assert {f["key"] for f in ops["filters"]} == {"alert_kind"}
    assert ops["filterMatch"] == "any" and ops["actionMatch"] == "any"


def test_alert_kind_is_the_tag_emit_alert_sets(repo_root: Path) -> None:
    text = (repo_root / "apps/api/app/core/alerts.py").read_text(encoding="utf-8")
    assert 'scope.set_tag("alert_kind", kind)' in text


def test_first_run_creates_every_rule(sr: ModuleType) -> None:
    fake = FakeSentry([{"id": "9", "name": "someone else's rule"}])
    assert run_main(sr, fake) == 0
    assert [c[0] for c in fake.calls] == ["GET", "POST", "POST", "POST", "POST"]
    assert all(c[1] == BASE and c[2] == TOKEN for c in fake.calls)
    assert fake.rules[0] == {"id": "9", "name": "someone else's rule"}


def test_rerun_keeps_every_rule(sr: ModuleType) -> None:
    fake = FakeSentry()
    assert run_main(sr, fake) == 0
    # Sentry answers with extra keys and numbers as text: still the same rule
    for rule in fake.rules:
        rule["dateCreated"] = "2026-11-09T00:00:00Z"
        for cond in rule["conditions"]:
            cond["name"] = "a label"
            if "value" in cond:
                cond["value"] = str(cond["value"])
    fake.calls.clear()
    assert run_main(sr, fake) == 0
    assert [c[0] for c in fake.calls] == ["GET"]


def test_changed_rule_is_updated_in_place(sr: ModuleType) -> None:
    fake = FakeSentry()
    assert run_main(sr, fake) == 0
    fake.rules[2]["conditions"][0]["value"] = 5
    fake.rules[0]["environment"] = None
    fake.calls.clear()
    assert run_main(sr, fake) == 0
    assert [(c[0], c[1]) for c in fake.calls] == [
        ("GET", BASE),
        ("PUT", f"{BASE}1/"),
        ("PUT", f"{BASE}3/"),
    ]


def test_slack_action_joins_every_rule(sr: ModuleType) -> None:
    fake = FakeSentry()
    extra = ("--slack-workspace", "12345", "--slack-channel", "#kuinn-alerts")
    assert run_main(sr, fake, *extra) == 0
    for rule in fake.rules:
        assert rule["actions"][1] == {
            "id": "sentry.integrations.slack.notify_action.SlackNotifyServiceAction",
            "workspace": "12345",
            "channel": "#kuinn-alerts",
        }


def test_duplicate_owned_rule_refused(sr: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeSentry(
        [{"id": "1", "name": "kuinn: new issue"}, {"id": "2", "name": "kuinn: new issue"}]
    )
    assert run_main(sr, fake) == 1
    assert [c[0] for c in fake.calls] == ["GET"]
    assert "delete the extras" in capsys.readouterr().err


def test_api_error_exits_1_without_the_token(
    sr: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeSentry(fail=403)
    assert run_main(sr, fake) == 1
    out = capsys.readouterr()
    assert "HTTP 403" in out.err and TOKEN not in out.out + out.err


def test_network_error_is_named_by_class(
    sr: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    def down(method: str, url: str, token: str, body: bytes | None) -> tuple[int, bytes]:
        raise OSError("connection refused")

    assert int(sr.main([*ARGS, "--token-stdin"], stdin=io.StringIO(TOKEN), transport=down)) == 1
    assert "OSError" in capsys.readouterr().err


@pytest.mark.parametrize("token", ["", "two words"], ids=["empty", "space"])
def test_bad_token_refused_before_any_call(sr: ModuleType, token: str) -> None:
    fake = FakeSentry()
    assert run_main(sr, fake, token=token) == 2
    assert fake.calls == []


@pytest.mark.parametrize(
    "bad",
    [
        ("--org", "Kuinn Org"),
        ("--api-url", "http://de.sentry.io"),
        ("--api-url", "https://sentry.example.com"),
        ("--environment", "prod uction"),
        ("--slack-workspace", "12345"),
        ("--slack-workspace", "abc", "--slack-channel", "#x"),
        ("--slack-workspace", "1", "--slack-channel", "kuinn"),
    ],
)
def test_bad_arguments_exit_2(sr: ModuleType, bad: tuple[str, ...]) -> None:
    args = list(ARGS)
    for key, value in zip(bad[::2], bad[1::2], strict=True):
        if key in args:
            args[args.index(key) + 1] = value
        else:
            args += [key, value]
    with pytest.raises(SystemExit) as exc:
        sr.main([*args, "--dry-run"], transport=FakeSentry())
    assert exc.value.code == 2


def test_dry_run_prints_rules_and_contacts_nothing(
    sr: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeSentry()
    assert sr.main([*ARGS, "--dry-run"], transport=fake) == 0
    assert fake.calls == []
    out = capsys.readouterr().out
    assert out.count("sentry: rule 'kuinn: ") == 4 and "nothing contacted" in out


# ---- deploy.sh sentry-alerts ----


def sentry_env(toolkit: Path) -> None:
    set_env(
        toolkit,
        "prod",
        SENTRY_ORG="kuinn",
        SENTRY_PROJECT="aimos-api",
        SENTRY_API_URL="https://de.sentry.io",
    )


def test_usage_lists_sentry_alerts(toolkit: Path, tmp_path: Path) -> None:
    result = run_dry(toolkit, tmp_path, "prod", "nope")
    assert result.returncode == 2
    assert re.search(r"^  sentry-alerts ", result.stderr, re.MULTILINE)
    # the usage block still ends with the edge lines (printed up to the bare `#`)
    assert result.stderr.rstrip().endswith("cors and status, ADR-0025)")


def test_sentry_alerts_refuses_blank_settings(toolkit: Path, tmp_path: Path) -> None:
    live = run_live(toolkit, tmp_path, "prod", "sentry-alerts")
    assert live.result.returncode != 0
    assert "set SENTRY_ORG SENTRY_PROJECT SENTRY_API_URL" in live.out
    assert live.calls == []


def test_sentry_alerts_refuses_bad_settings_before_gcloud(toolkit: Path, tmp_path: Path) -> None:
    sentry_env(toolkit)
    set_env(toolkit, "prod", SENTRY_API_URL="http://de.sentry.io")
    live = run_live(toolkit, tmp_path, "prod", "sentry-alerts")
    assert live.result.returncode != 0 and "bad Sentry settings" in live.out
    assert live.calls == []


def test_sentry_alerts_dry_run(toolkit: Path, tmp_path: Path) -> None:
    sentry_env(toolkit)
    result = run_dry(toolkit, tmp_path, "prod", "sentry-alerts")
    assert result.returncode == 0, result.stderr
    out = result.stdout + result.stderr
    assert out.count("sentry: rule 'kuinn: ") == 4
    create = [x for x in gcloud_lines(out) if "secrets create" in x]
    assert len(create) == 1 and "secrets create SENTRY_ALERTS_TOKEN" in create[0]
    assert "--locations=asia-south1" in create[0]
    assert "| sentry_rules.py --org kuinn" in out and "--token-stdin" in out


def test_sentry_alerts_stops_until_the_token_has_a_value(toolkit: Path, tmp_path: Path) -> None:
    sentry_env(toolkit)
    live = run_live(
        toolkit,
        tmp_path,
        "prod",
        "sentry-alerts",
        responses=[{"match": "secrets describe SENTRY_ALERTS_TOKEN", "stdout": "x\n"}],
    )
    assert live.result.returncode != 0
    assert "stopped until SENTRY_ALERTS_TOKEN has a value" in live.out
    assert "read -rsp" in live.out and "gcloud secrets versions add SENTRY_ALERTS_TOKEN" in live.out
    assert live.index("secrets create") == -1
    assert live.index("versions access") == -1


FAKE_HELPER = r"""#!/bin/sh
# Records the helper's argv and stdin; the dry run goes to the real interpreter.
case " $* " in
  *" --dry-run "*) exec "$REAL_PYTHON" "$@" ;;
esac
printf '%s\n' "$*" >> "$HELPER_LOG"
cat > "$HELPER_STDIN"
echo "sentry: 4 created, 0 updated, 0 kept"
"""


def test_sentry_alerts_pipes_the_token_into_the_helper(toolkit: Path, tmp_path: Path) -> None:
    sentry_env(toolkit)
    set_env(toolkit, "prod", SENTRY_SLACK_WORKSPACE="12345", SENTRY_SLACK_CHANNEL="#kuinn-alerts")
    _fake(tmp_path / "bin", "fake-helper-python", FAKE_HELPER)
    live = run_live(
        toolkit,
        tmp_path,
        "prod",
        "sentry-alerts",
        responses=[
            {"match": "secrets describe SENTRY_ALERTS_TOKEN", "stdout": "x\n"},
            {"match": "secrets versions list SENTRY_ALERTS_TOKEN", "stdout": "1\n"},
            {"match": "secrets versions access latest", "stdout": TOKEN},
        ],
        extra={
            "DEPLOY_PYTHON": str(tmp_path / "bin" / "fake-helper-python"),
            "REAL_PYTHON": sys.executable,
            "HELPER_LOG": str(tmp_path / "helper.log"),
            "HELPER_STDIN": str(tmp_path / "helper.stdin"),
        },
    )
    assert live.result.returncode == 0, live.out
    access = live.calls[live.index("secrets versions access")]
    assert "--secret=SENTRY_ALERTS_TOKEN" in access and "--project=aimos-prod" in access
    (argv,) = (tmp_path / "helper.log").read_text(encoding="utf-8").splitlines()
    assert argv.endswith("--slack-workspace 12345 --slack-channel #kuinn-alerts --token-stdin")
    assert "--environment production" in argv and TOKEN not in argv
    assert (tmp_path / "helper.stdin").read_text(encoding="utf-8") == TOKEN
    assert TOKEN not in live.out
    assert live.index("secrets create") == -1


def test_runbook_covers_sentry_alerts(repo_root: Path) -> None:
    text = (repo_root / "docs/runbooks/DEPLOY-GCP.md").read_text(encoding="utf-8")
    assert "## Sentry alert rules" in text and "`sentry-alerts`" in text
    for needle in (
        "alerts:write",
        "SENTRY_ALERTS_TOKEN",
        "SENTRY_API_URL",
        "kuinn: ops alert repeats",
    ):
        assert needle in text, needle
