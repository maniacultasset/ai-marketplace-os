"""DPP-2: `deploy.sh <env> edge ...` (the global external load balancer, Cloud Armor, the ingress
lock, alerts) and the edge-aware parts of `deploy`/`smoke`. Nothing here contacts Google Cloud or
the network: `gcloud` and `curl` are fakes on PATH. Dry runs use a fake gcloud that must never be
called; live runs use a stateful fake that logs every call and answers from a JSON rule list."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from tests.support.armor import export_of

DEPLOY = "scripts/deploy/gcp"
SHA = "0123456789ab"
ORIGIN = "https://app.example.com"
EDGE = "https://api.kuinn.in"
LOCKED = "internal-and-cloud-load-balancing"
RUN_APP = "https://aimos-api-abc123-el.a.run.app"
TAG_URL = "https://sha-0123456789ab---aimos-api-abc123-el.a.run.app"
CANDIDATE_URL = "https://candidate---aimos-api-abc123-el.a.run.app"
EDGE_SUBS = (
    "setup",
    "rules",
    "enforce",
    "lock-ingress",
    "unlock-ingress",
    "alerts",
    "cors",
    "status",
)

DRY_GCLOUD = """#!/bin/sh
case " $* " in *" --help "*) exit 0 ;; esac
echo "$*" >> "$FAKE_GCLOUD_LOG"
exit 99
"""

# Logs every call, then answers from the first matching rule of $FAKE_GCLOUD_RULES:
# {"match": substring (or a list, all must match), "stdout": str, "rc": int, "file": str written
# to --file-name=, "max": calls this rule answers, "unless_log": substring of earlier calls}.
# With no rule: describe/list/export fail (absent), everything else succeeds.
LIVE_GCLOUD = r"""#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
line = " ".join(argv)
log = os.environ["FAKE_GCLOUD_LOG"]
before = open(log, encoding="utf-8").read() if os.path.exists(log) else ""
with open(log, "a", encoding="utf-8") as handle:
    handle.write(line + "\n")
state_path = os.environ["FAKE_GCLOUD_STATE"]
state = json.load(open(state_path)) if os.path.exists(state_path) else {}
for index, rule in enumerate(json.load(open(os.environ["FAKE_GCLOUD_RULES"]))):
    match = rule["match"] if isinstance(rule["match"], list) else [rule["match"]]
    if not all(m in line for m in match):
        continue
    if "unless_log" in rule and rule["unless_log"] in before:
        continue
    if "if_log" in rule and rule["if_log"] not in before:
        continue
    used = state.get(str(index), 0)
    if "max" in rule and used >= rule["max"]:
        continue
    state[str(index)] = used + 1
    json.dump(state, open(state_path, "w"))
    if "file" in rule:
        name = next((a.split("=", 1)[1] for a in argv if a.startswith("--file-name=")), None)
        if name == "/dev/stdout" or name is None:
            sys.stdout.write(rule["file"])
        else:
            open(name, "w", encoding="utf-8").write(rule["file"])
    sys.stdout.write(rule.get("stdout", ""))
    sys.exit(rule.get("rc", 0))
words = set(argv)
sys.exit(1 if words & {"describe", "list", "export", "list-configs"} else 0)
"""

# Logs every call, then answers from the first rule of $FAKE_CURL_RULES whose "url" the request URL
# ends with (and whose "header", when given, is one of the -H values; "no_header" requires none):
# {"code": "401", "body": str, "headers": str written to -D, "rc": int, "if_log"/"unless_log":
# substring of the gcloud log}. With no rule: exit 7 (connection refused).
FAKE_CURL = r"""#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_CURL_LOG"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(argv) + "\n")
headers, dump, write_code, url, i = [], None, False, None, 0
while i < len(argv):
    a = argv[i]
    if a == "-H":
        headers.append(argv[i + 1]); i += 2; continue
    if a == "-D":
        dump = argv[i + 1]; i += 2; continue
    if a == "-w":
        write_code = True; i += 2; continue
    if a in ("-o", "--max-time"):
        i += 2; continue
    if not a.startswith("-"):
        url = a
    i += 1
glog = os.environ["FAKE_GCLOUD_LOG"]
gcalls = open(glog, encoding="utf-8").read() if os.path.exists(glog) else ""
for rule in json.load(open(os.environ["FAKE_CURL_RULES"])):
    if not url or not url.endswith(rule["url"]):
        continue
    if "header" in rule and rule["header"] not in headers:
        continue
    if rule.get("no_header") and headers:
        continue
    if "if_log" in rule and rule["if_log"] not in gcalls:
        continue
    if "unless_log" in rule and rule["unless_log"] in gcalls:
        continue
    if rule.get("rc"):
        sys.exit(rule["rc"])
    if dump:
        open(dump, "w").write(rule.get("headers", ""))
    sys.stdout.write(rule.get("code", "200") if write_code else rule.get("body", ""))
    sys.exit(0)
if write_code:
    sys.stdout.write("000")
sys.exit(7)
"""


@pytest.fixture
def toolkit(repo_root: Path, tmp_path: Path) -> Iterator[Path]:
    """A copy of scripts/deploy/gcp with the origins filled in (regular files only)."""
    target = tmp_path / "repo" / DEPLOY
    target.mkdir(parents=True)
    for source in (repo_root / DEPLOY).iterdir():
        if not source.is_file():
            continue
        text = source.read_text(encoding="utf-8")
        if source.suffix == ".env":
            text = text.replace("__FILL_ME__", ORIGIN)
            text = re.sub(r"^ALLOWED_ORIGINS=.*$", f"ALLOWED_ORIGINS={ORIGIN}", text, flags=re.M)
            text = re.sub(
                r"^DASHBOARD_BASE_URL=.*$", f"DASHBOARD_BASE_URL={ORIGIN}", text, flags=re.M
            )
        (target / source.name).write_text(text, encoding="utf-8")
    (target / "deploy.sh").chmod(0o755)
    yield target


def set_env(toolkit: Path, name: str, **keys: str) -> None:
    """Rewrite KEY=value lines of the copied <name>.env (the value is written as given)."""
    path = toolkit / f"{name}.env"
    text = path.read_text(encoding="utf-8")
    for key, value in keys.items():
        text, count = re.subn(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
        assert count == 1, key
    path.write_text(text, encoding="utf-8")


def _env(tmp_path: Path, bin_dir: Path, extra: dict[str, str] | None) -> dict[str, str]:
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "FAKE_GCLOUD_LOG": str(tmp_path / "gcloud.log"),
        "FAKE_GCLOUD_STATE": str(tmp_path / "gcloud.state"),
        "FAKE_GCLOUD_RULES": str(tmp_path / "gcloud.json"),
        "FAKE_CURL_LOG": str(tmp_path / "curl.log"),
        "FAKE_CURL_RULES": str(tmp_path / "curl.json"),
        "SMOKE_TRIES": "1",
        "SMOKE_SLEEP": "0",
        "DEPLOY_PYTHON": sys.executable,
    }
    env.update(extra or {})
    return env


def _fake(bin_dir: Path, name: str, text: str) -> None:
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / name
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _exec(
    toolkit: Path, tmp_path: Path, env: dict[str, str], args: tuple[str, ...], interpreter: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [interpreter, str(toolkit / "deploy.sh"), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
        cwd=tmp_path,
    )


def run_dry(
    toolkit: Path,
    tmp_path: Path,
    *args: str,
    extra: dict[str, str] | None = None,
    interpreter: str = "bash",
) -> subprocess.CompletedProcess[str]:
    """`deploy.sh ARGS --dry-run --sha SHA`; asserts that no gcloud command ran."""
    bin_dir = tmp_path / "bin"
    _fake(bin_dir, "gcloud", DRY_GCLOUD)
    _fake(bin_dir, "curl", FAKE_CURL)
    (tmp_path / "curl.json").write_text("[]", encoding="utf-8")
    env = _env(tmp_path, bin_dir, extra)
    result = _exec(toolkit, tmp_path, env, (*args, "--dry-run", "--sha", SHA), interpreter)
    log = tmp_path / "gcloud.log"
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    assert calls == "", f"gcloud ran under --dry-run: {calls}"
    curls = tmp_path / "curl.log"
    assert not curls.exists(), f"curl ran under --dry-run: {curls.read_text(encoding='utf-8')}"
    return result


class Live:
    """The result of a live run: the process, the gcloud calls and the curl calls."""

    def __init__(self, result: subprocess.CompletedProcess[str], tmp_path: Path) -> None:
        self.result = result
        self.out = result.stdout + result.stderr
        glog = tmp_path / "gcloud.log"
        clog = tmp_path / "curl.log"
        self.calls = glog.read_text(encoding="utf-8").splitlines() if glog.exists() else []
        self.curls = clog.read_text(encoding="utf-8").splitlines() if clog.exists() else []

    def index(self, *parts: str) -> int:
        """The position of the first gcloud call holding every part (-1 if none)."""
        for i, call in enumerate(self.calls):
            if all(p in call for p in parts):
                return i
        return -1


def run_live(
    toolkit: Path,
    tmp_path: Path,
    *args: str,
    responses: list[dict[str, Any]] | None = None,
    curl: list[dict[str, Any]] | None = None,
    extra: dict[str, str] | None = None,
) -> Live:
    """`deploy.sh ARGS --sha SHA` against the stateful fakes (no --dry-run)."""
    bin_dir = tmp_path / "bin"
    _fake(bin_dir, "gcloud", LIVE_GCLOUD)
    _fake(bin_dir, "curl", FAKE_CURL)
    (tmp_path / "gcloud.json").write_text(json.dumps(responses or []), encoding="utf-8")
    (tmp_path / "curl.json").write_text(json.dumps(curl or []), encoding="utf-8")
    env = _env(tmp_path, bin_dir, extra)
    result = _exec(toolkit, tmp_path, env, (*args, "--sha", SHA), "bash")
    return Live(result, tmp_path)


def gcloud_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("+ gcloud ")]


def blocks(output: str, header: str) -> list[str]:
    """The indented bodies deploy.sh prints after each `deploy: <header> <path>:` line."""
    found: list[str] = []
    lines = output.splitlines()
    for start, line in enumerate(lines):
        if not line.startswith(f"deploy: {header} "):
            continue
        body = []
        for inner in lines[start + 1 :]:
            if not inner.startswith("    "):
                break
            body.append(inner[4:])
        found.append("\n".join(body))
    assert found, header
    return found


def edge_ready(toolkit: Path) -> None:
    """prod.env as at the lock: ingress locked and the API on the edge domain."""
    set_env(toolkit, "prod", INGRESS=LOCKED, API_PUBLIC_URL=EDGE)


# ---- T1: plumbing ----


def test_usage_lists_edge(toolkit: Path, tmp_path: Path) -> None:
    result = run_dry(toolkit, tmp_path, "staging", "nope")
    assert result.returncode == 2
    assert re.search(r"^  edge ", result.stderr, re.MULTILINE)


@pytest.mark.parametrize("sub", EDGE_SUBS)
def test_staging_edge_refused(toolkit: Path, tmp_path: Path, sub: str) -> None:
    result = run_dry(toolkit, tmp_path, "staging", "edge", sub)
    assert result.returncode != 0
    assert "EDGE_DOMAIN" in result.stderr
    assert not gcloud_lines(result.stdout + result.stderr)


@pytest.mark.parametrize("args", [(), ("nope",), ("",)])
def test_edge_unknown_or_missing_sub(toolkit: Path, tmp_path: Path, args: tuple[str, ...]) -> None:
    result = run_dry(toolkit, tmp_path, "prod", "edge", *args)
    assert result.returncode == 2, result.stderr
    assert "unbound variable" not in result.stderr
    assert re.search(r"^  edge ", result.stderr, re.MULTILINE)


def test_prod_required_apis_include_edge(toolkit: Path, tmp_path: Path) -> None:
    prod = run_dry(toolkit, tmp_path, "prod", "preflight").stdout
    assert "compute.googleapis.com" in prod and "monitoring.googleapis.com" in prod
    staging = run_dry(toolkit, tmp_path, "staging", "preflight").stdout
    assert "compute.googleapis.com" not in staging
    assert "monitoring.googleapis.com" not in staging


@pytest.mark.parametrize("sub", EDGE_SUBS)
def test_edge_dry_runs_run_no_gcloud(toolkit: Path, tmp_path: Path, sub: str) -> None:
    if sub == "alerts":
        set_env(toolkit, "prod", ALERT_EMAIL="ops@kuinn.in")
    if sub == "lock-ingress":
        edge_ready(toolkit)
    result = run_dry(toolkit, tmp_path, "prod", "edge", sub)
    assert result.returncode == 0, result.stderr
    assert gcloud_lines(result.stdout + result.stderr), result.stdout


# ---- T3: edge setup ----

SETUP_ORDER = (
    "services enable compute.googleapis.com monitoring.googleapis.com",
    "compute addresses create aimos-api-edge-ip",
    "compute network-endpoint-groups create aimos-api-edge-neg",
    "compute network-endpoint-groups create aimos-api-edge-candidate-neg",
    "compute security-policies create kuinn-api-waf",
    "compute security-policies update kuinn-api-waf",
    "compute backend-services create aimos-api-edge-be",
    "compute backend-services create aimos-api-edge-candidate-be",
    "compute url-maps import aimos-api-edge-map",
    "compute ssl-policies create aimos-api-edge-ssl",
    "compute ssl-certificates create aimos-api-edge-cert",
    "compute target-https-proxies create aimos-api-edge-proxy",
    "compute forwarding-rules create aimos-api-edge-https",
)


def _line(lines: list[str], part: str) -> str:
    return next(line for line in lines if part in line)


def test_edge_setup_dry_run_order(toolkit: Path, tmp_path: Path) -> None:
    result = run_dry(toolkit, tmp_path, "prod", "edge", "setup")
    assert result.returncode == 0, result.stderr
    lines = gcloud_lines(result.stdout)
    positions = [next(i for i, line in enumerate(lines) if part in line) for part in SETUP_ORDER]
    assert positions == sorted(positions)
    neg = _line(lines, "network-endpoint-groups create aimos-api-edge-neg")
    candidate = _line(lines, "network-endpoint-groups create aimos-api-edge-candidate-neg")
    for line in (neg, candidate):
        assert "--region=asia-south1" in line and "--cloud-run-service=aimos-api" in line
        assert "--network-endpoint-type=serverless" in line
    assert "--cloud-run-tag=candidate" in candidate and "--cloud-run-tag" not in neg
    policy = _line(lines, "security-policies update kuinn-api-waf")
    for flag in ("--json-parsing=STANDARD", "--enable-layer7-ddos-defense", "--log-level=NORMAL"):
        assert flag in policy, flag
    assert "--type=CLOUD_ARMOR" in _line(lines, "security-policies create kuinn-api-waf")
    for name in ("aimos-api-edge-be", "aimos-api-edge-candidate-be"):
        create = _line(lines, f"backend-services create {name} ")
        for flag in (
            "--load-balancing-scheme=EXTERNAL_MANAGED",
            "--enable-logging",
            "--logging-sample-rate=1",
        ):
            assert flag in create, flag
        update = _line(lines, f"backend-services update {name} ")
        assert "--security-policy=kuinn-api-waf" in update and "--enable-logging" in update
    adds = [line for line in lines if "add-backend" in line]
    assert any(
        "aimos-api-edge-be " in a and "--network-endpoint-group=aimos-api-edge-neg " in a
        for a in adds
    )
    assert any("--network-endpoint-group=aimos-api-edge-candidate-neg " in a for a in adds)
    ssl = _line(lines, "ssl-policies create")
    assert "--profile=MODERN" in ssl and "--min-tls-version=1.2" in ssl
    assert "--domains=api.kuinn.in" in _line(lines, "ssl-certificates create")
    assert "--ssl-policy=aimos-api-edge-ssl" in _line(lines, "target-https-proxies create")
    rule = _line(lines, "forwarding-rules create")
    assert "--ports=443" in rule and "--load-balancing-scheme=EXTERNAL_MANAGED" in rule
    assert "point an A record for api.kuinn.in at <IP>" in result.stdout
    assert "pki.goog" in result.stdout


def test_edge_setup_has_no_http_or_verbose(toolkit: Path, tmp_path: Path) -> None:
    out = run_dry(toolkit, tmp_path, "prod", "edge", "setup").stdout
    assert "--ports=80" not in out and "VERBOSE" not in out
    assert "--custom-request-header" not in out


def test_edge_url_map_routes_candidate(toolkit: Path, tmp_path: Path) -> None:
    out = run_dry(toolkit, tmp_path, "prod", "edge", "setup").stdout
    url_map: dict[str, Any] = yaml.safe_load(blocks(out, "url map")[0])
    assert "routeRules" not in url_map
    assert url_map["name"] == "aimos-api-edge-map"
    assert url_map["defaultService"].endswith("/aimos-api-edge-be")
    assert url_map["hostRules"] == [{"hosts": ["*"], "pathMatcher": "edge"}]
    (matcher,) = url_map["pathMatchers"]
    assert matcher["name"] == "edge" and matcher["defaultService"].endswith("/aimos-api-edge-be")
    (route,) = matcher["routeRules"]
    assert route["priority"] == 1
    assert route["service"].endswith("/aimos-api-edge-candidate-be")
    (rule,) = route["matchRules"]
    assert rule["prefixMatch"] == "/"
    assert rule["headerMatches"] == [{"headerName": "x-aimos-candidate", "exactMatch": "1"}]
    assert url_map["defaultService"].startswith("projects/aimos-prod/global/backendServices/")


NEG_URLS = (
    "https://www.googleapis.com/compute/v1/projects/aimos-prod/regions/asia-south1/networkEndpointGroups/aimos-api-edge-neg;"
    "https://www.googleapis.com/compute/v1/projects/aimos-prod/regions/asia-south1/networkEndpointGroups/aimos-api-edge-candidate-neg\n"
)


def test_edge_setup_rerun_creates_nothing(toolkit: Path, tmp_path: Path) -> None:
    live = run_live(
        toolkit,
        tmp_path,
        "prod",
        "edge",
        "setup",
        responses=[
            {"match": "value(backends[].group)", "stdout": NEG_URLS},
            {"match": "addresses describe", "stdout": "203.0.113.10\n"},
            {"match": "describe", "stdout": "present\n"},
        ],
    )
    assert live.result.returncode == 0, live.out
    assert not [c for c in live.calls if " create " in f" {c} " or "add-backend" in c]
    for part in (
        "security-policies update kuinn-api-waf",
        "backend-services update aimos-api-edge-be",
        "url-maps import aimos-api-edge-map",
        "ssl-policies update aimos-api-edge-ssl",
        "target-https-proxies update aimos-api-edge-proxy",
    ):
        assert live.index(part) >= 0, part
    assert "point an A record for api.kuinn.in at 203.0.113.10" in live.out


def test_edge_setup_half_built_converges(toolkit: Path, tmp_path: Path) -> None:
    live = run_live(
        toolkit,
        tmp_path,
        "prod",
        "edge",
        "setup",
        responses=[
            {"match": "addresses describe aimos-api-edge-ip", "stdout": "203.0.113.10\n"},
            {"match": "network-endpoint-groups describe aimos-api-edge-neg ", "stdout": "x\n"},
        ],
    )
    assert live.result.returncode == 0, live.out
    creates = [c for c in live.calls if " create " in c]
    assert creates[0].startswith(
        "compute network-endpoint-groups create aimos-api-edge-candidate-neg"
    )
    assert live.index("addresses create") == -1
    assert live.index("network-endpoint-groups create aimos-api-edge-neg ") == -1


def test_edge_setup_stops_on_failed_create(toolkit: Path, tmp_path: Path) -> None:
    live = run_live(
        toolkit,
        tmp_path,
        "prod",
        "edge",
        "setup",
        responses=[{"match": "security-policies create", "rc": 1}],
    )
    assert live.result.returncode != 0
    position = live.index("security-policies create")
    assert position >= 0
    later = live.calls[position + 1 :]
    assert not [
        c
        for c in later
        if "backend-services" in c or "target-https-proxies" in c or "forwarding-rules" in c
    ]


# ---- T4: edge rules / edge enforce ----


@pytest.fixture
def ar(script: Callable[[str], ModuleType]) -> ModuleType:
    return script("scripts/deploy/gcp/armor_rules.py")


def policy_export(ar: ModuleType, repo_root: Path, enforce: list[str], **kw: Any) -> str:
    rules = ar.load(repo_root / DEPLOY / "armor-rules.yaml")
    return json.dumps(export_of(ar, ar.render(rules, enforce), **kw))


def rule_lines(output: str) -> list[str]:
    return [line for line in gcloud_lines(output) if "security-policies rules " in line]


def test_edge_rules_dry_run_all_preview(toolkit: Path, tmp_path: Path) -> None:
    result = run_dry(toolkit, tmp_path, "prod", "edge", "rules")
    assert result.returncode == 0, result.stderr
    creates = [line for line in rule_lines(result.stdout) if " rules create " in line]
    assert len(creates) == 13
    assert all("--preview" in line for line in creates)
    assert "security-policies import" not in result.stdout + result.stderr


def test_edge_enforce_path_only(toolkit: Path, tmp_path: Path) -> None:
    set_env(toolkit, "prod", EDGE_ENFORCE="path")
    result = run_dry(toolkit, tmp_path, "prod", "edge", "enforce")
    assert result.returncode == 0, result.stderr
    creates = [line for line in rule_lines(result.stdout) if " rules create " in line]
    enforced = {line.split()[6] for line in creates if "--preview" not in line}
    assert enforced == {"100", "110"}
    assert "preview → enforce: path" in result.stdout


@pytest.mark.parametrize(
    "enforce",
    ['"path crs-sqli"', "rate", "nope"],
    ids=["sqli-before-rate", "rate-alone", "unknown"],
)
@pytest.mark.parametrize("sub", ["rules", "enforce"])
def test_edge_enforce_out_of_order_refused(
    toolkit: Path, tmp_path: Path, enforce: str, sub: str
) -> None:
    set_env(toolkit, "prod", EDGE_ENFORCE=enforce)
    live = run_live(toolkit, tmp_path, "prod", "edge", sub)
    assert live.result.returncode != 0
    assert not [c for c in live.calls if "export" in c or " rules " in c]


def test_edge_rules_rerun_no_change(
    toolkit: Path, tmp_path: Path, ar: ModuleType, repo_root: Path
) -> None:
    live = run_live(
        toolkit,
        tmp_path,
        "prod",
        "edge",
        "rules",
        responses=[{"match": "security-policies export", "file": policy_export(ar, repo_root, [])}],
    )
    assert live.result.returncode == 0, live.out
    assert "already matches" in live.out
    assert not [c for c in live.calls if re.search(r" rules (create|update|delete) ", c)]


def test_edge_rules_preview_to_enforce(
    toolkit: Path, tmp_path: Path, ar: ModuleType, repo_root: Path
) -> None:
    set_env(toolkit, "prod", EDGE_ENFORCE="path")
    live = run_live(
        toolkit,
        tmp_path,
        "prod",
        "edge",
        "rules",
        responses=[
            {
                "match": "security-policies export",
                "file": policy_export(ar, repo_root, []),
                "max": 1,
            },
            {"match": "security-policies export", "file": policy_export(ar, repo_root, ["path"])},
        ],
    )
    assert live.result.returncode == 0, live.out
    updates = [c for c in live.calls if " rules update " in c]
    assert [u.split()[4] for u in updates] == ["100", "110"]
    assert all("--no-preview" in u for u in updates)
    exports = [i for i, c in enumerate(live.calls) if "security-policies export" in c]
    assert len(exports) == 2 and exports[1] > live.index("rules update 110")


def test_edge_rules_removes_stray_priority(
    toolkit: Path, tmp_path: Path, ar: ModuleType, repo_root: Path
) -> None:
    rules = ar.load(repo_root / DEPLOY / "armor-rules.yaml")
    stray = ar.RuleSpec(
        priority=500, description="x", expression="true", action="deny-403", preview=True
    )
    with_stray = json.dumps(export_of(ar, [*ar.render(rules, []), stray]))
    live = run_live(
        toolkit,
        tmp_path,
        "prod",
        "edge",
        "rules",
        responses=[
            {"match": "security-policies export", "file": with_stray, "max": 1},
            {"match": "security-policies export", "file": policy_export(ar, repo_root, [])},
        ],
    )
    assert live.result.returncode == 0, live.out
    assert live.index("rules delete 500") >= 0
    assert live.index("rules delete 2147483647") == -1


def test_edge_rules_partial_failure_exits(toolkit: Path, tmp_path: Path) -> None:
    live = run_live(
        toolkit,
        tmp_path,
        "prod",
        "edge",
        "rules",
        responses=[
            {"match": "security-policies export", "file": json.dumps({"rules": []})},
            {"match": "rules create 110 ", "rc": 1},
        ],
    )
    assert live.result.returncode != 0
    assert "partial apply at priority 110" in live.out and "rerun edge rules" in live.out
    position = live.index("rules create 110 ")
    assert position >= 0
    assert not [c for c in live.calls[position + 1 :] if " rules " in c]


def test_edge_rules_verify_catches_unchanged(
    toolkit: Path, tmp_path: Path, ar: ModuleType, repo_root: Path
) -> None:
    set_env(toolkit, "prod", EDGE_ENFORCE="path")
    live = run_live(
        toolkit,
        tmp_path,
        "prod",
        "edge",
        "rules",
        responses=[{"match": "security-policies export", "file": policy_export(ar, repo_root, [])}],
    )
    assert live.result.returncode != 0
    assert "100: a rule meant to be enforced is still in preview" in live.out


# ---- T5: deploy under INGRESS; T6: the JWT_ISSUER pin ----

OK_BODY = '{"status":"ok","checks":{"postgres":"ok"}}'
HEADER = "x-aimos-candidate: 1"


def service_doc(
    *, ingress: str = "all", issuer: str = RUN_APP, tags: dict[str, str] | None = None
) -> str:
    return json.dumps(
        {
            "metadata": {"annotations": {"run.googleapis.com/ingress": ingress}},
            "spec": {
                "template": {
                    "spec": {"containers": [{"env": [{"name": "JWT_ISSUER", "value": issuer}]}]}
                }
            },
            "status": {
                "url": RUN_APP,
                "traffic": [
                    {"revisionName": "r1", "percent": 100},
                    *({"tag": t, "url": u} for t, u in (tags or {}).items()),
                ],
            },
        }
    )


def existing_service(doc: str) -> list[dict[str, Any]]:
    return [
        {"match": "value(status.url)", "stdout": RUN_APP + "\n"},
        {"match": ["run services describe", "--format=json"], "stdout": doc},
        {"match": "images describe", "stdout": "sha256:" + "a" * 64 + "\n"},
    ]


def healthy(url: str, *, header: str | None = None, **extra: Any) -> list[dict[str, Any]]:
    cond: dict[str, Any] = {"header": header} if header else {}
    cond.update(extra)
    return [
        {"url": f"{url}/api/v1/auth/me", "code": "401", **cond},
        {"url": f"{url}/readyz", "body": OK_BODY, **cond},
    ]


@pytest.mark.parametrize("mode", ["all", "locked"])
def test_deploy_dry_run_ingress_flag(toolkit: Path, tmp_path: Path, mode: str) -> None:
    if mode == "locked":
        edge_ready(toolkit)
    out = run_dry(toolkit, tmp_path, "prod", "deploy").stdout
    deploy = next(line for line in out.splitlines() if line.startswith("+ gcloud run deploy "))
    want = "all" if mode == "all" else LOCKED
    assert f"--ingress={want} " in deploy


def test_deploy_unlocked_existing_unchanged(toolkit: Path, tmp_path: Path) -> None:
    live = run_live(
        toolkit,
        tmp_path,
        "prod",
        "deploy",
        responses=existing_service(service_doc(tags={"sha-0123456789ab": TAG_URL})),
        curl=[*healthy(TAG_URL, no_header=True), *healthy(RUN_APP, no_header=True)],
    )
    assert live.result.returncode == 0, live.out
    deploy = live.calls[live.index("run deploy aimos-api")]
    assert "--ingress=all" in deploy and "--no-traffic" in deploy
    assert "--tag=sha-0123456789ab" in deploy and "candidate" not in deploy
    shift = live.index("update-traffic", "--to-latest")
    assert shift > live.index("run deploy aimos-api")
    assert live.curls[0].endswith(f"{TAG_URL}/api/v1/auth/me")
    assert live.curls[1].endswith(f"{TAG_URL}/readyz")
    assert not [c for c in live.curls if HEADER in c]


def test_deploy_locked_smoke_pass_then_shift(toolkit: Path, tmp_path: Path) -> None:
    edge_ready(toolkit)
    live = run_live(
        toolkit,
        tmp_path,
        "prod",
        "deploy",
        responses=existing_service(service_doc(ingress=LOCKED, issuer=EDGE)),
        curl=[*healthy(EDGE, header=HEADER), *healthy(EDGE, if_log="--to-latest")],
    )
    assert live.result.returncode == 0, live.out
    deploy = live.calls[live.index("run deploy aimos-api")]
    assert f"--ingress={LOCKED}" in deploy
    assert "--no-traffic" in deploy and "--tag=candidate" in deploy
    assert f"-H {HEADER} " in live.curls[0] and live.curls[0].endswith(f"{EDGE}/api/v1/auth/me")
    assert live.curls[1].endswith(f"-H {HEADER} {EDGE}/readyz")
    assert live.index("update-traffic", "--to-latest") >= 0
    assert live.index("--remove-tags") == -1


def test_deploy_locked_smoke_fail_keeps_traffic(toolkit: Path, tmp_path: Path) -> None:
    edge_ready(toolkit)
    live = run_live(
        toolkit,
        tmp_path,
        "prod",
        "deploy",
        responses=existing_service(service_doc(ingress=LOCKED, issuer=EDGE)),
        curl=[{"url": "/api/v1/auth/me", "code": "503"}],
    )
    assert live.result.returncode != 0
    assert "failed smoke; traffic stays" in live.out
    assert live.index("update-traffic", "--remove-tags=candidate") > live.index(
        "run deploy aimos-api"
    )
    assert live.index("--to-latest") == -1


@pytest.mark.parametrize(
    "value", ["internal", '""', '"all "'], ids=["internal", "empty", "all-space"]
)
def test_deploy_bad_ingress_refused(toolkit: Path, tmp_path: Path, value: str) -> None:
    set_env(toolkit, "prod", INGRESS=value)
    result = run_dry(toolkit, tmp_path, "prod", "deploy")
    assert result.returncode != 0
    assert "INGRESS must be" in result.stderr
    assert not gcloud_lines(result.stdout + result.stderr)


def test_deploy_locked_without_edge_domain_refused(toolkit: Path, tmp_path: Path) -> None:
    set_env(toolkit, "staging", INGRESS=LOCKED)
    result = run_dry(toolkit, tmp_path, "staging", "deploy")
    assert result.returncode != 0 and "EDGE_DOMAIN" in result.stderr
    assert not gcloud_lines(result.stdout + result.stderr)


@pytest.mark.parametrize(
    "url", ["", "https://other.example", "http://api.kuinn.in"], ids=["blank", "other", "http"]
)
def test_deploy_locked_needs_public_url_on_edge(toolkit: Path, tmp_path: Path, url: str) -> None:
    set_env(toolkit, "prod", INGRESS=LOCKED, API_PUBLIC_URL=url)
    result = run_dry(toolkit, tmp_path, "prod", "deploy")
    assert result.returncode != 0
    assert "API_PUBLIC_URL" in result.stderr
    assert not gcloud_lines(result.stdout + result.stderr)


def test_deploy_refuses_unlock_of_locked_service(toolkit: Path, tmp_path: Path) -> None:
    responses = existing_service(service_doc(ingress=LOCKED, tags={"sha-0123456789ab": TAG_URL}))
    curl = [*healthy(TAG_URL), *healthy(RUN_APP)]
    refused = run_live(toolkit, tmp_path, "prod", "deploy", responses=responses, curl=curl)
    assert refused.result.returncode != 0
    assert "unlock-ingress" in refused.out
    assert refused.index("run deploy") == -1
    (tmp_path / "gcloud.log").unlink()
    (tmp_path / "gcloud.state").unlink(missing_ok=True)
    allowed = run_live(
        toolkit,
        tmp_path,
        "prod",
        "deploy",
        responses=responses,
        curl=curl,
        extra={"DEPLOY_ALLOW_UNLOCK": "1"},
    )
    assert allowed.result.returncode == 0, allowed.out


def test_deploy_refuses_issuer_change(toolkit: Path, tmp_path: Path) -> None:
    set_env(toolkit, "prod", API_PUBLIC_URL=EDGE)
    old = "https://aimos-api-x-el.a.run.app"
    responses = existing_service(service_doc(issuer=old, tags={"sha-0123456789ab": TAG_URL}))
    curl = [*healthy(TAG_URL), *healthy(EDGE)]

    def fresh() -> None:
        for name in ("gcloud.log", "gcloud.state", "curl.log"):
            (tmp_path / name).unlink(missing_ok=True)

    refused = run_live(toolkit, tmp_path, "prod", "deploy", responses=responses, curl=curl)
    assert refused.result.returncode != 0
    assert "JWT_ISSUER" in refused.out and old in refused.out
    assert refused.index("run deploy") == -1
    fresh()
    forced = run_live(
        toolkit,
        tmp_path,
        "prod",
        "deploy",
        responses=responses,
        curl=curl,
        extra={"DEPLOY_ALLOW_ISSUER_CHANGE": "1"},
    )
    assert forced.result.returncode == 0, forced.out
    fresh()
    set_env(toolkit, "prod", JWT_ISSUER=old)
    pinned = run_live(toolkit, tmp_path, "prod", "deploy", responses=responses, curl=curl)
    assert pinned.result.returncode == 0, pinned.out


def test_deploy_live_guard_skipped_first_deploy(toolkit: Path, tmp_path: Path) -> None:
    live = run_live(
        toolkit,
        tmp_path,
        "prod",
        "deploy",
        responses=[{"match": "images describe", "stdout": "sha256:" + "a" * 64 + "\n"}],
        curl=healthy(RUN_APP),
    )
    assert "live-state guard: skipped" in live.out
    assert live.index("run deploy aimos-api") >= 0


def test_release_refuses_before_build(toolkit: Path, tmp_path: Path) -> None:
    set_env(toolkit, "prod", INGRESS="internal")
    result = run_dry(toolkit, tmp_path, "prod", "release")
    assert result.returncode != 0 and "INGRESS must be" in result.stderr
    assert "builds submit" not in result.stdout


def test_smoke_header_empty_bash32(toolkit: Path, tmp_path: Path) -> None:
    version = subprocess.run(
        ["/bin/bash", "-c", "echo $BASH_VERSION"], capture_output=True, text=True, check=False
    ).stdout
    if not version.startswith("3.2"):
        pytest.skip("/bin/bash is not Bash 3.2")
    result = run_dry(toolkit, tmp_path, "prod", "smoke", interpreter="/bin/bash")
    assert result.returncode == 0, result.stderr
    assert "unbound variable" not in result.stderr
    edge = run_dry(toolkit, tmp_path, "prod", "edge", "status", interpreter="/bin/bash")
    assert edge.returncode == 0 and "unbound variable" not in edge.stderr


def test_smoke_edge_domain_uses_auth_me(toolkit: Path, tmp_path: Path) -> None:
    set_env(toolkit, "prod", API_PUBLIC_URL=EDGE)
    out = run_dry(toolkit, tmp_path, "prod", "smoke").stdout
    assert f"{EDGE}/api/v1/auth/me" in out and "/healthz" not in out


def _api_env(output: str) -> dict[str, str]:
    loaded = yaml.safe_load(blocks(output, "env file")[0])
    assert isinstance(loaded, dict)
    return {str(k): str(v) for k, v in loaded.items()}


def test_issuer_pinned(toolkit: Path, tmp_path: Path) -> None:
    pinned = "https://aimos-api-x-el.a.run.app"
    set_env(toolkit, "prod", JWT_ISSUER=pinned, API_PUBLIC_URL=EDGE)
    env = _api_env(run_dry(toolkit, tmp_path, "prod", "deploy").stdout)
    assert env["JWT_ISSUER"] == pinned
    assert env["AMAZON_OAUTH_REDIRECT_URI"] == f"{EDGE}/api/v1/marketplaces/amazon/callback"
    assert env["FLIPKART_OAUTH_REDIRECT_URI"] == f"{EDGE}/api/v1/marketplaces/flipkart/callback"


def test_issuer_blank_follows_url(toolkit: Path, tmp_path: Path) -> None:
    set_env(toolkit, "prod", API_PUBLIC_URL=EDGE)
    env = _api_env(run_dry(toolkit, tmp_path, "prod", "deploy").stdout)
    assert env["JWT_ISSUER"] == EDGE


def test_issuer_pinned_env_boots(
    toolkit: Path, tmp_path: Path, clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    set_env(toolkit, "prod", JWT_ISSUER="https://aimos-api-x-el.a.run.app", API_PUBLIC_URL=EDGE)
    out = run_dry(toolkit, tmp_path, "prod", "deploy").stdout
    for key, value in _api_env(out).items():
        monkeypatch.setenv(key, value)
    secrets = {
        "DATABASE_URL": "postgresql+psycopg://app_rw:" + "a" * 48 + "@/marketplace_os"
        "?host=/cloudsql/aimos-prod:asia-south1:aimos-db",
        "JWT_SECRET": "b" * 64,
        "ADMIN_SESSION_SECRET": "c" * 64,
        "S3_ACCESS_KEY": "GOOG1EXAMPLEACCESSID",
        "S3_SECRET_KEY": "d" * 40,
        "SENTRY_DSN": "https://key@o0.ingest.sentry.io/0",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    assert get_settings().jwt_issuer == "https://aimos-api-x-el.a.run.app"


@pytest.mark.parametrize("pin", ["https://aimos-api-x-el.a.run.app", ""], ids=["pinned", "blank"])
def test_first_deploy_repoint_skips_pinned_issuer(toolkit: Path, tmp_path: Path, pin: str) -> None:
    set_env(toolkit, "prod", JWT_ISSUER=pin)
    live = run_live(
        toolkit,
        tmp_path,
        "prod",
        "deploy",
        responses=[
            {"match": "images describe", "stdout": "sha256:" + "a" * 64 + "\n"},
            {"match": "projects describe", "stdout": "123\n"},
            {
                "match": "value(status.url)",
                "stdout": RUN_APP + "\n",
                "if_log": "run deploy aimos-api",
            },
        ],
        curl=healthy(RUN_APP),
    )
    assert live.result.returncode == 0, live.out
    update = live.calls[live.index("run services update aimos-api", "--update-env-vars")]
    assert f"AMAZON_OAUTH_REDIRECT_URI={RUN_APP}/api/v1/marketplaces/amazon/callback" in update
    assert ("JWT_ISSUER=" in update) is (pin == "")


def test_issuer_http_refused(toolkit: Path, tmp_path: Path) -> None:
    set_env(toolkit, "prod", JWT_ISSUER="http://x")
    result = run_dry(toolkit, tmp_path, "prod", "deploy")
    assert result.returncode != 0 and "JWT_ISSUER" in result.stderr
    assert not gcloud_lines(result.stdout + result.stderr)


# ---- T7: edge lock-ingress / unlock-ingress ----

LOCK = f"--ingress={LOCKED}"
CSP = f"default-src 'self'; connect-src 'self' {EDGE} https://storage.googleapis.com/aimos-images-prod/"


def spa(
    *, bundle: str = f'fetch("{EDGE}/api/v1")', csp: str = CSP, acao: str = ORIGIN
) -> list[dict[str, Any]]:
    """The SPA origin, its bundle and the edge /readyz it calls (with that origin's CORS grant)."""
    return [
        {
            "url": f"{ORIGIN}/",
            "body": '<script type="module" src="/assets/index-Ab1.js"></script>',
            "headers": f"HTTP/2 200\r\ncontent-type: text/html\r\ncontent-security-policy: {csp}\r\n",
        },
        {"url": f"{ORIGIN}/assets/index-Ab1.js", "body": bundle},
        {
            "url": f"{EDGE}/readyz",
            "header": f"Origin: {ORIGIN}",
            "body": OK_BODY,
            "headers": f"HTTP/2 200\r\naccess-control-allow-origin: {acao}\r\n",
        },
        {
            "url": f"{ORIGIN}/readyz",
            "body": "<html></html>",
            "headers": "content-type: text/html\r\n",
        },
    ]


def direct(url: str, *, after: str = "404") -> list[dict[str, Any]]:
    """URL answers the API (401) until the lock, then AFTER."""
    return [
        {"url": f"{url}/api/v1/auth/me", "code": "401", "unless_log": LOCK},
        {"url": f"{url}/api/v1/auth/me", "code": after},
    ]


LOCK_DOC = service_doc(
    ingress="all", issuer=EDGE, tags={"sha-0123456789ab": TAG_URL, "candidate": CANDIDATE_URL}
)


def lock(toolkit: Path, tmp_path: Path, curl: list[dict[str, Any]]) -> Live:
    edge_ready(toolkit)
    return run_live(
        toolkit,
        tmp_path,
        "prod",
        "edge",
        "lock-ingress",
        responses=[{"match": ["run services describe", "--format=json"], "stdout": LOCK_DOC}],
        curl=curl,
    )


def test_lock_refuses_env_all(toolkit: Path, tmp_path: Path) -> None:
    set_env(toolkit, "prod", API_PUBLIC_URL=EDGE)
    live = run_live(
        toolkit, tmp_path, "prod", "edge", "lock-ingress", curl=[*spa(), *healthy(EDGE)]
    )
    assert live.result.returncode != 0
    assert "set INGRESS" in live.out
    assert live.index("services update") == -1


def test_lock_refuses_failed_lb_smoke(toolkit: Path, tmp_path: Path) -> None:
    live = lock(toolkit, tmp_path, [{"url": f"{EDGE}/api/v1/auth/me", "code": "503"}, *spa()])
    assert live.result.returncode != 0
    assert live.index("services update") == -1


@pytest.mark.parametrize(
    "case",
    ["bundle-lacks-edge", "csp-lacks-edge", "no-acao", "grace-readyz-fails"],
)
def test_lock_refuses_spa_not_moved(toolkit: Path, tmp_path: Path, case: str) -> None:
    rules = {
        "bundle-lacks-edge": spa(bundle=f'fetch("{RUN_APP}/api/v1")'),
        "csp-lacks-edge": spa(csp=f"default-src 'self'; connect-src 'self' {RUN_APP}"),
        "no-acao": spa(acao="https://other.example"),
        "grace-readyz-fails": [
            {
                "url": f"{ORIGIN}/readyz",
                "body": '{"status":"down"}',
                "headers": "content-type: application/json\r\n",
            },
            *spa(),
        ],
    }[case]
    live = lock(toolkit, tmp_path, [*healthy(EDGE, no_header=True), *rules])
    assert live.result.returncode != 0
    assert ORIGIN in live.result.stderr and "lock refused" in live.result.stderr
    assert live.index("services update") == -1


def test_lock_success(toolkit: Path, tmp_path: Path) -> None:
    live = lock(
        toolkit,
        tmp_path,
        [*healthy(EDGE, no_header=True), *spa(), *direct(RUN_APP), *direct(CANDIDATE_URL)],
    )
    assert live.result.returncode == 0, live.out
    remove = live.index("update-traffic", "--remove-tags=sha-0123456789ab")
    locked = live.index("run services update aimos-api", LOCK)
    assert 0 <= remove < locked
    assert "candidate" not in live.calls[remove]
    assert live.index("--ingress=all") == -1
    probes = [
        i for i, c in enumerate(live.curls) if c.endswith("/api/v1/auth/me") and "a.run.app" in c
    ]
    assert any(RUN_APP in live.curls[i] for i in probes)
    assert any(CANDIDATE_URL in live.curls[i] for i in probes)
    readyz = [i for i, c in enumerate(live.curls) if f"Origin: {ORIGIN}" in c]
    assert len(readyz) == 2 and readyz[1] > max(probes)


def test_lock_direct_still_open(toolkit: Path, tmp_path: Path) -> None:
    live = lock(
        toolkit,
        tmp_path,
        [
            *healthy(EDGE, no_header=True),
            *spa(),
            *direct(RUN_APP, after="401"),
            *direct(CANDIDATE_URL),
        ],
    )
    assert live.result.returncode != 0
    assert live.index("--ingress=all") > live.index(LOCK)
    assert "INGRESS" in live.result.stderr and "rolled back" in live.result.stderr


def test_lock_tag_url_still_open(toolkit: Path, tmp_path: Path) -> None:
    live = lock(
        toolkit,
        tmp_path,
        [
            *healthy(EDGE, no_header=True),
            *spa(),
            *direct(RUN_APP),
            *direct(CANDIDATE_URL, after="401"),
        ],
    )
    assert live.result.returncode != 0
    assert live.index("--ingress=all") > live.index(LOCK)
    assert CANDIDATE_URL in live.result.stderr


def test_lock_spa_broken_after_lock(toolkit: Path, tmp_path: Path) -> None:
    broken = {
        "url": f"{EDGE}/readyz",
        "header": f"Origin: {ORIGIN}",
        "body": "upstream error",
        "if_log": LOCK,
    }
    live = lock(
        toolkit,
        tmp_path,
        [broken, *healthy(EDGE, no_header=True), *spa(), *direct(RUN_APP), *direct(CANDIDATE_URL)],
    )
    assert live.result.returncode != 0
    assert live.index("--ingress=all") > live.index(LOCK)
    assert "rolled back" in live.result.stderr


def test_unlock_sets_all(toolkit: Path, tmp_path: Path) -> None:
    result = run_dry(toolkit, tmp_path, "prod", "edge", "unlock-ingress")
    assert result.returncode == 0
    assert "run services update aimos-api --region=asia-south1 --ingress=all" in result.stdout
    assert "set INGRESS=all" in result.stdout


# ---- T8: edge alerts ----


def test_alerts_refuses_blank_email(toolkit: Path, tmp_path: Path) -> None:
    live = run_live(toolkit, tmp_path, "prod", "edge", "alerts")
    assert live.result.returncode != 0 and "ALERT_EMAIL" in live.out
    assert live.calls == []


@pytest.mark.parametrize(
    "email", ['"a@b.c,d@e.f"', '"a b@c.d"', "x"], ids=["two", "space", "no-at"]
)
def test_alerts_refuses_bad_email(toolkit: Path, tmp_path: Path, email: str) -> None:
    set_env(toolkit, "prod", ALERT_EMAIL=email)
    live = run_live(toolkit, tmp_path, "prod", "edge", "alerts")
    assert live.result.returncode != 0 and "ALERT_EMAIL" in live.out
    assert live.calls == []


def alerts_dry(toolkit: Path, tmp_path: Path) -> str:
    set_env(toolkit, "prod", ALERT_EMAIL="ops@kuinn.in")
    result = run_dry(toolkit, tmp_path, "prod", "edge", "alerts")
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_alerts_dry_run_prints_all(toolkit: Path, tmp_path: Path) -> None:
    out = alerts_dry(toolkit, tmp_path)
    lines = gcloud_lines(out)
    assert len([x for x in lines if "logging metrics create" in x]) == 2
    channels = [x for x in lines if "beta monitoring channels create" in x]
    assert len(channels) == 1 and "--type=email" in channels[0]
    assert "email_address=ops@kuinn.in" in channels[0]
    policies = [x for x in lines if "monitoring policies create" in x]
    assert len(policies) == 5
    assert not [x for x in policies if " beta " in x or " alpha " in x]
    docs = "\n".join(blocks(out, "policy"))
    assert "uptime_check/check_passed" in docs and 'check_id=\\"<uptime-check-id>\\"' in docs
    assert "network_security_policy" in docs and "dos_attack" in docs
    uptime = next(x for x in lines if "monitoring uptime create" in x)
    assert "--path=/readyz" in uptime and "--protocol=https" in uptime
    assert "--resource-labels=host=api.kuinn.in\\,project_id=aimos-prod" in uptime
    assert "logging buckets create security-logs" in out and "--retention-days=400" in out
    sink = shlex.split(next(x for x in lines if "logging sinks create edge-security" in x))
    (log_filter,) = [a.split("=", 1)[1] for a in sink if a.startswith("--log-filter=")]
    assert 'NOT httpRequest.requestUrl:"/callback"' in log_filter
    assert 'jsonPayload.previewSecurityPolicy.outcome="DENY"' in log_filter
    assert "dos_attack" in log_filter and "previewSecurityPolicy:*" not in log_filter
    exclusion = shlex.split(next(x for x in lines if "sinks update _Default" in x))
    (add,) = [a for a in exclusion if a.startswith("--add-exclusion=")]
    assert add.startswith("--add-exclusion=name=edge-callback-urls,filter=")
    assert 'httpRequest.requestUrl:"/callback"' in add


def test_alerts_policy_files_valid(toolkit: Path, tmp_path: Path) -> None:
    out = alerts_dry(toolkit, tmp_path)
    docs = [json.loads(block) for block in blocks(out, "policy")]
    assert len(docs) == 5
    for doc in docs:
        assert doc["displayName"] and len(doc["conditions"]) == 1
        assert doc["notificationChannels"] == [
            "projects/aimos-prod/notificationChannels/<channel-id>"
        ]
        if "conditionMatchedLog" in doc["conditions"][0]:
            assert doc["alertStrategy"]["notificationRateLimit"]["period"] == "300s"
    assert sum("conditionMatchedLog" in d["conditions"][0] for d in docs) == 1


ALERT_BASE: list[dict[str, Any]] = [
    {
        "match": "beta monitoring channels list",
        "stdout": "projects/aimos-prod/notificationChannels/9\n",
    },
]


def alerts_live(toolkit: Path, tmp_path: Path, responses: list[dict[str, Any]]) -> Live:
    set_env(toolkit, "prod", ALERT_EMAIL="ops@kuinn.in")
    return run_live(toolkit, tmp_path, "prod", "edge", "alerts", responses=responses)


def test_alerts_beta_missing(toolkit: Path, tmp_path: Path) -> None:
    live = alerts_live(toolkit, tmp_path, [{"match": "channels create --help", "rc": 1}])
    assert live.result.returncode != 0
    assert "components install beta" in live.out
    assert not [c for c in live.calls if " create " in c and "--help" not in c]


def test_alerts_uptime_id_from_server(toolkit: Path, tmp_path: Path) -> None:
    live = alerts_live(
        toolkit,
        tmp_path,
        [*ALERT_BASE, {"match": "uptime list-configs", "stdout": "aimos-edge-readyz-a1b2c3\n"}],
    )
    assert live.result.returncode == 0, live.out
    docs = [json.loads(b) for b in blocks(live.result.stdout, "policy")]
    filters = [d["conditions"][0].get("conditionThreshold", {}).get("filter", "") for d in docs]
    assert any('check_id="aimos-edge-readyz-a1b2c3"' in f for f in filters)
    assert not any('check_id="aimos-edge-readyz"' in f for f in filters)
    assert live.index("monitoring uptime create") == -1


@pytest.mark.parametrize(
    "answer", ["", "aimos-edge-readyz-a\naimos-edge-readyz-b\n"], ids=["empty", "two-lines"]
)
def test_alerts_uptime_id_must_be_one(toolkit: Path, tmp_path: Path, answer: str) -> None:
    live = alerts_live(
        toolkit, tmp_path, [*ALERT_BASE, {"match": "uptime list-configs", "stdout": answer}]
    )
    assert live.result.returncode != 0
    assert live.index("policies create") == -1


def test_alerts_rerun_creates_nothing(toolkit: Path, tmp_path: Path) -> None:
    live = alerts_live(
        toolkit,
        tmp_path,
        [
            *ALERT_BASE,
            {"match": "uptime list-configs", "stdout": "aimos-edge-readyz-a1b2c3\n"},
            {
                "match": "monitoring policies list",
                "stdout": "projects/aimos-prod/alertPolicies/1\n",
            },
            {"match": "thresholdValue", "stdout": "50.0\n"},
            {"match": "sinks describe _Default", "stdout": "edge-callback-urls\n"},
            {"match": "describe", "stdout": "present\n"},
        ],
    )
    assert live.result.returncode == 0, live.out
    assert not [c for c in live.calls if " create " in c and "--help" not in c]
    assert live.index("policies update") == -1
    assert live.index("sinks update _Default") == -1


def test_alerts_deny_threshold_updated_in_place(toolkit: Path, tmp_path: Path) -> None:
    set_env(toolkit, "prod", ALERT_EMAIL="ops@kuinn.in")
    live = run_live(
        toolkit,
        tmp_path,
        "prod",
        "edge",
        "alerts",
        responses=[
            *ALERT_BASE,
            {"match": "uptime list-configs", "stdout": "aimos-edge-readyz-a1b2c3\n"},
            {
                "match": "monitoring policies list",
                "stdout": "projects/aimos-prod/alertPolicies/1\n",
            },
            {"match": "thresholdValue", "stdout": "50.0\n"},
        ],
        extra={"EDGE_DENY_ALERT": "10"},
    )
    assert live.result.returncode == 0, live.out
    update = live.calls[live.index("monitoring policies update")]
    assert "policy-deny.json" in update
    assert len([c for c in live.calls if "policies update" in c]) == 1


# ---- T9: edge cors / edge status ----


def test_edge_cors_matches_setup(toolkit: Path, tmp_path: Path) -> None:
    set_env(toolkit, "prod", ALLOWED_ORIGINS=f"{ORIGIN},\\ ,https://kuinn.in")
    setup = run_dry(toolkit, tmp_path, "prod", "setup")
    cors = run_dry(toolkit, tmp_path, "prod", "edge", "cors")
    assert setup.returncode == 0 and cors.returncode == 0, cors.stderr
    from_setup = json.loads(blocks(setup.stdout, "cors")[0])
    from_edge = json.loads(blocks(cors.stdout, "cors")[0])
    assert from_setup == from_edge
    assert from_edge[0]["origin"] == [ORIGIN, "https://kuinn.in"]
    assert len([x for x in gcloud_lines(cors.stdout) if "--cors-file=" in x]) == 2


def test_edge_cors_refuses_unfilled(toolkit: Path, tmp_path: Path) -> None:
    set_env(toolkit, "prod", ALLOWED_ORIGINS="__FILL_ME__")
    result = run_dry(toolkit, tmp_path, "prod", "edge", "cors")
    assert result.returncode != 0 and "ALLOWED_ORIGINS is not filled" in result.stderr


def test_edge_status_read_only(toolkit: Path, tmp_path: Path) -> None:
    live = run_live(toolkit, tmp_path, "prod", "edge", "status", responses=[{"match": "", "rc": 1}])
    assert live.result.returncode == 0, live.out
    assert live.calls
    assert all(" describe " in f" {c} " or " export " in f" {c} " for c in live.calls), live.calls
    assert "preview rules left: unknown" in live.out


def test_edge_status_serving_gate(
    toolkit: Path, tmp_path: Path, ar: ModuleType, repo_root: Path
) -> None:
    rules = ar.load(repo_root / DEPLOY / "armor-rules.yaml")
    full = yaml.safe_dump(export_of(ar, ar.render(rules, list(ar.GROUPS))))
    serving = run_live(
        toolkit,
        tmp_path,
        "prod",
        "edge",
        "status",
        responses=[{"match": "security-policies export", "file": full}],
        curl=[{"url": f"{EDGE}/api/v1/auth/me", "code": "401"}],
    )
    assert serving.result.returncode == 0, serving.out
    assert "edge serving: yes" in serving.out
    assert "preview rules left: 0" in serving.out
    for name in ("gcloud.log", "gcloud.state", "curl.log"):
        (tmp_path / name).unlink(missing_ok=True)
    preview = yaml.safe_dump(export_of(ar, ar.render(rules, ["path"])))
    down = run_live(
        toolkit,
        tmp_path,
        "prod",
        "edge",
        "status",
        responses=[{"match": "security-policies export", "file": preview}],
        curl=[{"url": f"{EDGE}/api/v1/auth/me", "rc": 60}],
    )
    assert "edge serving: no" in down.out
    assert "preview rules left: 11" in down.out


# ---- T11: docs ----


def test_edge_runbook_covers_subcommands(repo_root: Path) -> None:
    text = (repo_root / "docs/runbooks/EDGE-CUTOVER.md").read_text(encoding="utf-8")
    for sub in EDGE_SUBS:
        assert f"`{sub}`" in text or f"edge {sub}`" in text, sub
    lines = text.splitlines()
    steps = [i for i, line in enumerate(lines) if re.match(r"^\d+\. ", line)]
    assert len(steps) >= 15
    for n, start in enumerate(steps):
        end = steps[n + 1] if n + 1 < len(steps) else len(lines)
        assert any("Rollback:" in line for line in lines[start:end]), lines[start]
    for needle in (
        "x-aimos-candidate",
        "xff_entries",
        "JWT_ISSUER",
        "not merged",
        "same window",
        "unlock-ingress",
        "preview rules left: 0",
        "mkos-lanes/gcp",
        "Production",
    ):
        assert needle in text, needle


def test_deploy_runbook_lists_edge(repo_root: Path) -> None:
    text = (repo_root / "docs/runbooks/DEPLOY-GCP.md").read_text(encoding="utf-8")
    for needle in (
        "`edge`",
        "INGRESS",
        "DEPLOY_ALLOW_UNLOCK",
        "DEPLOY_ALLOW_ISSUER_CHANGE",
        "EDGE-CUTOVER.md",
    ):
        assert needle in text, needle


def test_adr_0022_ledger(repo_root: Path) -> None:
    (adr,) = (repo_root / "docs/adr").glob("0022-*.md")
    assert "Status:** proposed" in adr.read_text(encoding="utf-8").splitlines()[2]
    assert "| 0022 |" in (repo_root / "docs/adr/README.md").read_text(encoding="utf-8")
