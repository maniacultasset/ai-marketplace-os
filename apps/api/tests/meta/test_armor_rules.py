"""DPP-2 T2: scripts/deploy/gcp/armor-rules.yaml and its helper armor_rules.py (validate, render,
plan, verify). Offline: the plan is checked as argv, never run."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, get_args
from urllib.parse import unquote

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.armor import export_of
from tests.support.paths import repo_root as find_repo_root

HELPER = "scripts/deploy/gcp/armor_rules.py"
RULES = "scripts/deploy/gcp/armor-rules.yaml"
DEFAULT = 2147483647
POLICY = "--security-policy=kuinn-api-waf"


@pytest.fixture(scope="module")
def ar(script: Callable[[str], ModuleType]) -> ModuleType:
    return script(HELPER)


@pytest.fixture(scope="module")
def rules(ar: ModuleType) -> list[Any]:
    loaded: list[Any] = ar.load(find_repo_root(Path(__file__)) / RULES)
    return loaded


def by_priority(rules: list[Any], priority: int) -> Any:
    return next(r for r in rules if r.priority == priority)


def regex_of(match: str) -> str:
    found = re.search(r"matches\('([^']*)'\)", match)
    assert found is not None, match
    return found.group(1)


# ---- a tiny CEL subset: request.path[.urlDecode()], request.method, matches, !=, ==, !, && ----


def cel(expression: str, method: str, path: str) -> bool:
    tokens: list[str] = re.findall(
        r"&&|!=|==|!|'[^']*'|request\.\w+(?:\.urlDecode\(\))?|\.matches\(|\)", expression
    )
    assert "".join(tokens).replace(" ", "") == expression.replace(" ", ""), expression
    position = 0

    def peek() -> str | None:
        return tokens[position] if position < len(tokens) else None

    def take() -> str:
        nonlocal position
        position += 1
        return tokens[position - 1]

    def subject() -> str:
        name = take()
        return {
            "request.path": path,
            "request.path.urlDecode()": unquote(path),
            "request.method": method,
        }[name]

    def unary() -> bool:
        if peek() == "!":
            take()
            return not unary()
        value = subject()
        op = take()
        if op == ".matches(":
            pattern = take()[1:-1]
            assert take() == ")"
            return re.search(pattern, value) is not None
        literal = take()[1:-1]
        return value != literal if op == "!=" else value == literal

    result = unary()
    while peek() == "&&":
        take()
        right = unary()
        result = result and right
    assert peek() is None, expression
    return result


# ---- the committed file ----


def test_repo_file_is_valid(ar: ModuleType, rules: list[Any]) -> None:
    priorities = [r.priority for r in rules]
    assert len(priorities) == len(set(priorities))
    groups = {r.group for r in rules}
    assert groups == set(ar.GROUPS)
    crs = [r for r in rules if r.group.startswith("crs-")]
    rate = [r for r in rules if r.group == "rate"]
    ordered = sorted(crs, key=lambda r: ar.GROUPS.index(r.group))
    assert [r.priority for r in ordered] == sorted(r.priority for r in crs)
    assert max(r.priority for r in crs) < min(r.priority for r in rate)


def test_no_methodenforcement(ar: ModuleType, repo_root: Path) -> None:
    assert "methodenforcement" not in (repo_root / RULES).read_text(encoding="utf-8")
    assert "crs-methodenforcement" not in ar.GROUPS
    assert not any("methodenforcement" in s for s in ar.CRS_SETS.values())


def test_method_rule_allows_served_methods(rules: list[Any]) -> None:
    from app.api.v1.surface import Method

    pattern = regex_of(by_priority(rules, 110).match)
    for method in (*get_args(Method), "HEAD", "OPTIONS"):
        assert re.search(pattern, method), method
    for method in ("PUT", "TRACE", "CONNECT", "get"):
        assert not re.search(pattern, method), method


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/listings/abc/content",
        "/api/v1/members/u1",
        "/api/v1/tenants/me",
        "/api/v1/brand/documents/d1",
    ],
)
def test_no_enforced_rule_denies_patch_delete(rules: list[Any], method: str, path: str) -> None:
    for rule in rules:
        if rule.action.startswith("deny") and rule.waf is None:
            assert not cel(rule.match, method, path), (rule.priority, method, path)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/listings",
        "/api/v1/marketplaces/amazon/callback",
        "/api/v1/admin/signup-requests",
        "/readyz",
    ],
)
def test_path_rule_regex_allowed(rules: list[Any], path: str) -> None:
    assert not cel(by_priority(rules, 100).match, "GET", path)


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/healthz",
        "/docs",
        "/openapi.json",
        "/admin",
        "/api/v2/x",
        "/readyz/x",
        "/api/v1",
        "//api/v1/x",
        "/%61pi/v1/x",
    ],
)
def test_path_rule_regex_denied(rules: list[Any], path: str) -> None:
    assert cel(by_priority(rules, 100).match, "GET", path)


AUTH_MATCH = [
    "/auth/login",
    "/auth/login/mfa",
    "/auth/login/mfa/enroll",
    "/auth/login/mfa/confirm",
    "/auth/password",
    "/auth/password/expired",
    "/auth/mfa/totp/confirm",
    "/auth/mfa/step-up",
    "/signup",
    "/signup/status",
]
AUTH_NO_MATCH = [
    "/auth/me",
    "/auth/refresh",
    "/auth/logout",
    "/auth/security",
    "/auth/mfa/totp/enroll",
    "/auth/mfa/recovery-codes",
    "/auth/login/a/b/c",
    "/auth/password/x",
    "/signupx",
    "/admin/signup-requests",
]


@pytest.mark.parametrize(
    "path", [*(f"/api/v1{p}" for p in AUTH_MATCH), "/api/v1/auth/l%6Fgin", "/api/v1/%61uth/login"]
)
def test_auth_regex_match(rules: list[Any], path: str) -> None:
    assert cel(by_priority(rules, 300).match, "POST", path)
    assert not cel(by_priority(rules, 300).match, "OPTIONS", path)


@pytest.mark.parametrize("path", AUTH_NO_MATCH)
def test_auth_regex_no_match(rules: list[Any], path: str) -> None:
    assert not cel(by_priority(rules, 300).match, "POST", f"/api/v1{path}")


def test_rate_and_crs_rules_use_decoded_path(rules: list[Any]) -> None:
    for rule in rules:
        if rule.rate is not None or rule.waf is not None:
            assert "request.path.matches(" not in rule.match, rule.priority
            if "request.path" in rule.match:
                assert "request.path.urlDecode().matches(" in rule.match, rule.priority
    assert "request.path.matches(" in by_priority(rules, 100).match


def crs_skip(rules: list[Any]) -> str:
    return regex_of(by_priority(rules, 200).match)


def test_multipart_regex_exact(rules: list[Any]) -> None:
    skip = crs_skip(rules)
    for path in ("brand/documents", "products/bulk", "images/upload"):
        assert re.search(skip, f"/api/v1/{path}"), path
    for path in ("images/upload-url", "brand/documents/123"):
        assert not re.search(skip, f"/api/v1/{path}"), path


def test_crs_skip_covers_secret_bodies(rules: list[Any]) -> None:
    skip = crs_skip(rules)
    for path in ("/api/v1/auth/login", "/api/v1/signup", "/api/v1/auth/refresh"):
        assert re.search(skip, path), path
    for path in AUTH_MATCH:
        assert re.search(skip, f"/api/v1{path}"), path
    assert not re.search(skip, "/api/v1/listings/x/content")
    assert len(skip) < 1024


def test_rate_rules_skip_options(rules: list[Any]) -> None:
    for rule in rules:
        if rule.action in ("throttle", "ban"):
            assert "request.method != 'OPTIONS'" in rule.match, rule.priority


def test_crs_exclusions(ar: ModuleType, rules: list[Any]) -> None:
    skip = crs_skip(rules)
    for spec in ar.render(rules, []):
        rule = next((r for r in rules if r.priority == spec.priority), None)
        if rule is None or not rule.group.startswith("crs-"):
            continue
        assert f"!request.path.urlDecode().matches('{skip}')" in spec.expression
        assert spec.exclusions == (ar.Exclusion(ar.CRS_SETS[rule.group], "authorization"),)


# ---- load() rejects ----

GOOD = """version: 1
patterns: {p: "^/api/v1/x$"}
rules:
  - priority: 100
    group: path
    description: d
    match: "!request.path.matches('^/api/')"
    action: deny-404
  - priority: 200
    group: crs-lfi
    description: d
    match: "!request.path.urlDecode().matches('${p}')"
    action: deny-403
    waf: {rule_set: lfi-v422-stable, sensitivity: 1, exclude_headers: [authorization]}
  - priority: 300
    group: rate
    description: d
    match: "request.method != 'OPTIONS'"
    action: throttle
    rate: {count: 5, interval_sec: 60}
"""


def write(tmp_path: Path, text: str | bytes) -> Path:
    path = tmp_path / "rules.yaml"
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def test_load_accepts_good(ar: ModuleType, tmp_path: Path) -> None:
    loaded = ar.load(write(tmp_path, GOOD))
    assert [r.priority for r in loaded] == [100, 200, 300]
    assert "^/api/v1/x$" in loaded[1].match


BAD = {
    "duplicate-priority": ("priority: 300", "priority: 100"),
    "priority-zero": ("priority: 100", "priority: 0"),
    "priority-default": ("priority: 100", "priority: 2147483647"),
    "priority-negative": ("priority: 100", "priority: -5"),
    "unknown-action": ("action: deny-404", "action: deny-418"),
    "unknown-group": ("group: path", "group: paths"),
    "throttle-no-options": ("match: \"request.method != 'OPTIONS'\"", 'match: "true"'),
    "rate-raw-path": (
        "match: \"request.method != 'OPTIONS'\"",
        "match: \"request.path.matches('^/x') && request.method != 'OPTIONS'\"",
    ),
    "crs-raw-path": ("request.path.urlDecode().matches('${p}')", "request.path.matches('${p}')"),
    "lookahead": ("'^/api/'", "'^/(?=api)'"),
    "lookbehind": ("'^/api/'", "'^/(?<!x)api'"),
    "backref": ("'^/api/'", "'^/(a)\\\\1'"),
    "unknown-crs-set": ("lfi-v422-stable", "lfi-v33-stable"),
    "methodenforcement": ("lfi-v422-stable", "methodenforcement-v422-stable"),
    "crs-priority-above-rate": ("priority: 200", "priority: 350"),
    "bad-opt-out": (
        "sensitivity: 1,",
        "sensitivity: 1, opt_out_rule_ids: [owasp-crs-v030301-id1],",
    ),
    "count-zero": ("count: 5", "count: 0"),
    "count-nan": ("count: 5", "count: .nan"),
    "count-inf": ("count: 5", "count: .inf"),
    "count-string": ("count: 5", 'count: "5"'),
    "count-bool": ("count: 5", "count: true"),
    "unknown-pattern": ("${p}", "${q}"),
    "unknown-key": ("action: throttle", "action: throttle\n    extra: 1"),
}


@pytest.mark.parametrize("case", sorted(BAD))
def test_load_rejects(ar: ModuleType, tmp_path: Path, case: str) -> None:
    old, new = BAD[case]
    assert old in GOOD, case
    with pytest.raises(ar.RulesError):
        ar.load(write(tmp_path, GOOD.replace(old, new, 1)))


def test_load_rejects_crs_out_of_group_order(ar: ModuleType, tmp_path: Path) -> None:
    extra = """  - priority: 190
    group: crs-xss
    description: d
    match: "!request.path.urlDecode().matches('${p}')"
    action: deny-403
    waf: {rule_set: xss-v422-stable, sensitivity: 1, exclude_headers: [authorization]}
"""
    with pytest.raises(ar.RulesError, match="GROUPS order"):
        ar.load(write(tmp_path, GOOD + extra))


def test_load_rejects_ban_without_duration(ar: ModuleType, tmp_path: Path) -> None:
    text = GOOD.replace("action: throttle", "action: ban")
    with pytest.raises(ar.RulesError, match="ban_duration_sec"):
        ar.load(write(tmp_path, text))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "version: 1\nrules: []\n",
        b"version: 1\nrules:\n  - description: caf\xe9\n",
        'version: 1\nrules: !!python/object/apply:os.system ["touch PWNED"]\n',
        "version: 2\nrules: []\n",
        "- 1\n",
    ],
    ids=["empty", "no-rules", "non-utf8", "python-object-tag", "version", "not-a-mapping"],
)
def test_load_rejects_documents(
    ar: ModuleType, tmp_path: Path, text: str | bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ar.RulesError):
        ar.load(write(tmp_path, text))
    assert not (tmp_path / "PWNED").exists()


def test_load_accepts_unicode_description(ar: ModuleType, tmp_path: Path) -> None:
    loaded = ar.load(write(tmp_path, GOOD.replace("description: d", "description: लॉगिन सीमा", 1)))
    assert loaded[0].description == "लॉगिन सीमा"


# ---- check_enforce ----

FULL = [
    "path",
    "rate",
    "crs-lfi",
    "crs-rfi",
    "crs-rce",
    "crs-protocolattack",
    "crs-scannerdetection",
    "crs-sqli",
    "crs-xss",
]


@pytest.mark.parametrize(
    "groups",
    [[], ["path"], ["path", "rate"], FULL, ["path", "rate", "crs-rce"]],
    ids=["none", "path", "path-rate", "full", "tier2-subset"],
)
def test_check_enforce_passes(ar: ModuleType, groups: list[str]) -> None:
    ar.check_enforce(groups)


@pytest.mark.parametrize(
    "groups",
    [
        ["rate"],
        ["path", "crs-sqli"],
        ["path", "rate", "crs-sqli"],
        ["path", "rate", "crs-lfi", "crs-rfi", "crs-rce", "crs-protocolattack", "crs-sqli"],
        ["path", "path"],
        ["nope"],
    ],
    ids=["rate-alone", "path-crs-sqli", "sqli-early", "scanner-missing", "duplicate", "unknown"],
)
def test_check_enforce_fails(ar: ModuleType, groups: list[str]) -> None:
    with pytest.raises(ar.RulesError):
        ar.check_enforce(groups)


@given(st.lists(st.sampled_from(FULL), max_size=12))
@settings(max_examples=300, deadline=None)
def test_check_enforce_tiers(ar: ModuleType, groups: list[str]) -> None:
    TIERS, RulesError, check_enforce = ar.TIERS, ar.RulesError, ar.check_enforce  # noqa: N806
    listed = set(groups)
    expected = len(groups) == len(listed) and all(
        all(other in listed for other in FULL if TIERS[other] < TIERS[group]) for group in groups
    )
    try:
        check_enforce(groups)
        passed = True
    except RulesError:
        passed = False
    assert passed == expected


# ---- render ----


def test_render_preview_flags(ar: ModuleType, rules: list[Any]) -> None:
    specs = ar.render(rules, [])
    assert all(s.preview for s in specs if s.priority != DEFAULT)
    assert not next(s for s in specs if s.priority == DEFAULT).preview
    path_only = ar.render(rules, ["path"])
    assert {s.priority for s in path_only if not s.preview} == {100, 110, DEFAULT}


def test_render_shapes(ar: ModuleType, rules: list[Any]) -> None:
    specs = {s.priority: s for s in ar.render(rules, [])}
    ban = specs[300]
    assert ban.action == "rate-based-ban"
    assert (ban.rate_count, ban.rate_interval_sec) == (20, 60)
    assert (ban.ban_count, ban.ban_interval_sec, ban.ban_duration_sec) == (60, 600, 900)
    assert (ban.conform_action, ban.exceed_action, ban.enforce_on_key) == (
        "allow",
        "deny-429",
        "ip",
    )
    assert specs[100].action == "deny-404"
    assert specs[100].rate_count is None
    assert specs[200].opt_out == ()
    assert specs[200].exclusions == (ar.Exclusion("lfi-v422-stable", "authorization"),)
    assert (
        "evaluatePreconfiguredWaf('lfi-v422-stable', {'sensitivity': 1})" in specs[200].expression
    )
    assert specs[DEFAULT].action == "allow" and specs[DEFAULT].expression is None


def test_render_opt_out(ar: ModuleType, tmp_path: Path) -> None:
    text = GOOD.replace(
        "sensitivity: 1,", "sensitivity: 1, opt_out_rule_ids: [owasp-crs-v042200-id930100-lfi],"
    )
    specs = {s.priority: s for s in ar.render(ar.load(write(tmp_path, text)), [])}
    assert (
        "{'sensitivity': 1, 'opt_out_rule_ids': ['owasp-crs-v042200-id930100-lfi']}"
        in specs[200].expression
    )
    assert specs[200].opt_out == ("owasp-crs-v042200-id930100-lfi",)


# ---- normalise_live / plan / verify ----


def default_only(ar: ModuleType, rules: list[Any]) -> list[Any]:
    return [s for s in ar.render(rules, []) if s.priority == DEFAULT]


def test_plan_equal_is_noop(ar: ModuleType, rules: list[Any]) -> None:
    rendered = ar.render(rules, ["path"])
    assert ar.plan(ar.normalise_live(export_of(ar, rendered)), rendered) == []


def test_plan_argv(ar: ModuleType, rules: list[Any]) -> None:
    rendered = ar.render(rules, [])
    argv = ar.plan(ar.normalise_live(export_of(ar, default_only(ar, rules))), rendered)
    creates = [a for a in argv if a[3] == "create"]
    assert sorted(int(a[4]) for a in creates) == sorted(r.priority for r in rules)
    assert all(a[:3] == ["compute", "security-policies", "rules"] and POLICY in a for a in argv)
    assert not [a for a in argv if a[3] == "delete"]
    (ban,) = [a for a in creates if a[4] == "300"]
    for flag in (
        "--action=rate-based-ban",
        "--rate-limit-threshold-count=20",
        "--rate-limit-threshold-interval-sec=60",
        "--ban-threshold-count=60",
        "--ban-threshold-interval-sec=600",
        "--ban-duration-sec=900",
        "--conform-action=allow",
        "--exceed-action=deny-429",
        "--enforce-on-key=ip",
        "--preview",
    ):
        assert flag in ban, flag
    (path,) = [a for a in creates if a[4] == "100"]
    assert "--action=deny-404" in path and "--preview" in path
    assert any(a.startswith("--expression=!request.path.matches(") for a in path)
    exclusions = [a for a in argv if a[3] == "add-preconfig-waf-exclusion"]
    assert sorted(int(a[4]) for a in exclusions) == [200, 210, 220, 230, 240, 250, 260]
    for a in exclusions:
        assert "--request-header-to-exclude=op=EQUALS,val=authorization" in a
        assert any(x.startswith("--target-rule-set=") and x.endswith("-v422-stable") for x in a)
        # each exclusion follows its own rule's create
        assert argv.index(a) > argv.index(next(c for c in creates if c[4] == a[4]))


def test_plan_preview_to_enforce(ar: ModuleType, rules: list[Any]) -> None:
    live = ar.normalise_live(export_of(ar, ar.render(rules, [])))
    argv = ar.plan(live, ar.render(rules, ["path"]))
    assert [(a[3], a[4]) for a in argv] == [("update", "100"), ("update", "110")]
    assert all("--no-preview" in a for a in argv)


def test_plan_removes_extra_priority_keeps_default(ar: ModuleType, rules: list[Any]) -> None:
    rendered = ar.render(rules, [])
    stray = ar.RuleSpec(
        priority=500, description="x", expression="true", action="deny-403", preview=True
    )
    argv = ar.plan(ar.normalise_live(export_of(ar, [*rendered, stray])), rendered)
    assert argv == [["compute", "security-policies", "rules", "delete", "500", POLICY]]
    no_default = [s for s in rendered if s.priority != DEFAULT]
    assert all(
        str(DEFAULT) not in a for a in ar.plan(ar.normalise_live(export_of(ar, no_default)), [])
    )


def test_plan_default_drift_is_updated(ar: ModuleType, rules: list[Any]) -> None:
    rendered = ar.render(rules, [])
    live = ar.normalise_live(export_of(ar, rendered))
    drifted = [
        (
            ar.RuleSpec(
                priority=DEFAULT, description="", expression=None, action="deny-403", preview=False
            )
            if s.priority == DEFAULT
            else s
        )
        for s in live
    ]
    argv = ar.plan(drifted, rendered)
    assert argv == [
        ["compute", "security-policies", "rules", "update", str(DEFAULT), POLICY, "--action=allow"]
    ]


def test_plan_order_creates_before_deletes(ar: ModuleType, rules: list[Any]) -> None:
    rendered = ar.render(rules, [])
    moved = [
        ar.RuleSpec(**{**s.__dict__, "priority": 270}) if s.priority == 260 else s for s in rendered
    ]
    argv = ar.plan(ar.normalise_live(export_of(ar, moved)), rendered)
    ops = [(a[3], a[4]) for a in argv]
    assert ops.index(("create", "260")) < ops.index(("delete", "270"))


def test_plan_action_kind_change_is_delete_then_create(ar: ModuleType, rules: list[Any]) -> None:
    rendered = ar.render(rules, [])
    changed = [
        (
            ar.RuleSpec(
                priority=300, description="x", expression="true", action="deny-403", preview=True
            )
            if s.priority == 300
            else s
        )
        for s in rendered
    ]
    argv = ar.plan(ar.normalise_live(export_of(ar, changed)), rendered)
    assert [(a[3], a[4]) for a in argv] == [("delete", "300"), ("create", "300")]


def test_plan_exclusion_change(ar: ModuleType, rules: list[Any]) -> None:
    rendered = ar.render(rules, [])
    changed = [
        (
            ar.RuleSpec(
                **{**s.__dict__, "exclusions": (ar.Exclusion("lfi-v422-stable", "cookie"),)}
            )
            if s.priority == 200
            else s
        )
        for s in rendered
    ]
    argv = ar.plan(ar.normalise_live(export_of(ar, changed)), rendered)
    assert [(a[3], a[4]) for a in argv] == [
        ("remove-preconfig-waf-exclusion", "200"),
        ("add-preconfig-waf-exclusion", "200"),
    ]
    assert "--request-header-to-exclude=op=EQUALS,val=cookie" in argv[0]


def test_verify_flags_preview_left(
    ar: ModuleType, rules: list[Any], repo_root: Path, tmp_path: Path
) -> None:
    live = tmp_path / "live.json"
    live.write_text(json.dumps(export_of(ar, ar.render(rules, ["path"]), preview={100: True})))
    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / HELPER),
            "verify",
            str(repo_root / RULES),
            "--enforce",
            "path",
            "--live",
            str(live),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 4
    assert "100" in result.stderr and "still in preview" in result.stderr
    assert "110" not in result.stderr
    live.write_text(json.dumps(export_of(ar, ar.render(rules, ["path"]))))
    ok = subprocess.run(
        [
            sys.executable,
            str(repo_root / HELPER),
            "verify",
            str(repo_root / RULES),
            "--enforce",
            "path",
            "--live",
            str(live),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr


def test_cli_plan_and_check(repo_root: Path, tmp_path: Path) -> None:
    out = tmp_path / "argv.txt"
    helper = [sys.executable, str(repo_root / HELPER)]
    plan = subprocess.run(
        [*helper, "plan", str(repo_root / RULES), "--enforce", "path,rate", "--out", str(out)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert plan.returncode == 0, plan.stderr
    lines = out.read_text(encoding="utf-8").splitlines()
    assert "\0" not in out.read_text(encoding="utf-8")
    parsed = [shlex.split(line) for line in lines]
    assert all(p[:3] == ["compute", "security-policies", "rules"] for p in parsed)
    bad = subprocess.run(
        [*helper, "check", str(repo_root / RULES), "--enforce", "rate"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 2 and "path" in bad.stderr
    summary = subprocess.run(
        [*helper, "summary", str(repo_root / RULES), "--enforce", "path"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "preview → enforce: path" in summary.stdout
    assert "still in preview: rate" in summary.stdout


# ---- plan converges: applying plan(x, r) to a model of x gives a state with nothing to do ----


def flag(argv: list[str], name: str) -> str | None:
    prefix = f"--{name}="
    return next((a[len(prefix) :] for a in argv if a.startswith(prefix)), None)


def _int(value: str | None) -> int | None:
    return None if value is None else int(value)


def apply(ar: ModuleType, state: dict[int, Any], argv_list: list[list[str]]) -> dict[int, Any]:
    """A model of what each gcloud argv does to the policy."""
    state = dict(state)
    for argv in argv_list:
        op, priority = argv[3], int(argv[4])
        if op == "delete":
            del state[priority]
            continue
        if op in ("add-preconfig-waf-exclusion", "remove-preconfig-waf-exclusion"):
            header = flag(argv, "request-header-to-exclude")
            assert header is not None
            item = ar.Exclusion(flag(argv, "target-rule-set"), header.split("val=", 1)[1])
            spec = state[priority]
            if op.startswith("add"):
                excl = (*spec.exclusions, item)
            else:
                excl = tuple(e for e in spec.exclusions if e != item)
            state[priority] = ar.RuleSpec(**{**spec.__dict__, "exclusions": excl})
            continue
        base: dict[str, Any] = (
            state[priority].__dict__
            if op == "update"
            else {
                "priority": priority,
                "description": "",
                "expression": None,
                "action": "allow",
                "preview": False,
                "exclusions": (),
            }
        )
        fields = dict(base)
        for key, name in (
            ("description", "description"),
            ("expression", "expression"),
            ("action", "action"),
        ):
            if flag(argv, name) is not None:
                fields[key] = flag(argv, name)
        if "--preview" in argv:
            fields["preview"] = True
        if "--no-preview" in argv:
            fields["preview"] = False
        if fields["action"] in ("throttle", "rate-based-ban"):
            fields.update(
                rate_count=_int(flag(argv, "rate-limit-threshold-count")),
                rate_interval_sec=_int(flag(argv, "rate-limit-threshold-interval-sec")),
                ban_count=_int(flag(argv, "ban-threshold-count")),
                ban_interval_sec=_int(flag(argv, "ban-threshold-interval-sec")),
                ban_duration_sec=_int(flag(argv, "ban-duration-sec")),
                conform_action=flag(argv, "conform-action"),
                exceed_action=flag(argv, "exceed-action"),
                enforce_on_key=flag(argv, "enforce-on-key"),
            )
        state[priority] = ar.RuleSpec(**fields)
    return state


@given(data=st.data())
@settings(max_examples=150, deadline=None)
def test_plan_idempotent(ar: ModuleType, rules: list[Any], data: st.DataObject) -> None:
    enforce = FULL[: data.draw(st.integers(0, len(FULL)))]
    rendered = ar.render(rules, enforce)
    live: dict[int, Any] = {}
    for spec in ar.render(rules, data.draw(st.sampled_from([[], ["path"], FULL]))):
        choice = data.draw(st.sampled_from(["keep", "drop", "flip", "renumber", "describe"]))
        if spec.priority == DEFAULT or choice == "keep":
            live[spec.priority] = spec
        elif choice == "flip":
            live[spec.priority] = ar.RuleSpec(**{**spec.__dict__, "preview": not spec.preview})
        elif choice == "renumber":
            live[spec.priority + 5] = ar.RuleSpec(
                **{**spec.__dict__, "priority": spec.priority + 5}
            )
        elif choice == "describe":
            live[spec.priority] = ar.RuleSpec(**{**spec.__dict__, "description": "old"})
    if data.draw(st.booleans()):
        live[300] = ar.RuleSpec(
            priority=300, description="x", expression="true", action="deny-403", preview=True
        )
    state = apply(ar, live, ar.plan(list(live.values()), rendered))
    assert ar.plan(list(state.values()), rendered) == []
