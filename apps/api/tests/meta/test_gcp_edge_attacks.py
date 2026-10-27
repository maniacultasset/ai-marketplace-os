"""DPP-2 tester attacks that the edge toolkit survives: shell injection through the rules file,
every served route against the Armor path and method rules, encoded auth paths, a failed /readyz
in the locked candidate smoke, and every `edge` dry run under Bash 3.2. Offline: fake gcloud and
curl only, never Google Cloud."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import unquote

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.meta.test_gcp_edge import (
    EDGE,
    EDGE_SUBS,
    HEADER,
    LOCKED,
    edge_ready,
    existing_service,
    gcloud_lines,
    run_dry,
    run_live,
    service_doc,
    set_env,
    toolkit,
)
from tests.support.paths import repo_root as find_repo_root

__all__ = ["toolkit"]
RULES = "scripts/deploy/gcp/armor-rules.yaml"


@pytest.fixture(scope="module")
def ar(script: Callable[[str], ModuleType]) -> ModuleType:
    return script("scripts/deploy/gcp/armor_rules.py")


@pytest.fixture(scope="module")
def rules(ar: ModuleType) -> list[Any]:
    loaded: list[Any] = ar.load(find_repo_root(Path(__file__)) / RULES)
    return loaded


def regex(rules: list[Any], priority: int) -> str:
    found = re.search(
        r"matches\('([^']*)'\)", next(r for r in rules if r.priority == priority).match
    )
    assert found is not None
    return found.group(1)


# ---- the rules file cannot run shell code through `edge rules` (eval of shlex lines) ----


@pytest.mark.parametrize(
    "payload",
    ["$(touch {m})", "`touch {m}`", "x'; touch {m}; echo '", 'x"; touch {m}; echo "'],
    ids=["dollar-paren", "backtick", "single-quote", "double-quote"],
)
def test_rules_text_is_never_executed(toolkit: Path, tmp_path: Path, payload: str) -> None:
    marker = tmp_path / "PWNED"
    text = (toolkit / "armor-rules.yaml").read_text(encoding="utf-8")
    hostile = payload.format(m=marker)
    text = text.replace(
        "description: methods the API serves, plus HEAD and OPTIONS",
        f"description: {yaml_scalar(hostile)}",
        1,
    )
    (toolkit / "armor-rules.yaml").write_text(text, encoding="utf-8")
    result = run_dry(toolkit, tmp_path, "prod", "edge", "rules")
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "a rules-file description ran as shell code"
    line = next(x for x in gcloud_lines(result.stdout) if " rules create 110 " in x)
    assert "touch" in line


def yaml_scalar(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


# ---- every served route passes rules 100 and 110; the CRS skip is exactly the listed routes ----


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000001", path)


def test_every_surface_route_passes_path_and_method_rules(rules: list[Any]) -> None:
    from app.api.v1.surface import OPS

    path_deny, method_allow = regex(rules, 100), regex(rules, 110)
    for op in OPS.values():
        path = "/api/v1" + _concrete(op.path)
        assert re.search(path_deny, path), (op.op_id, path)
        assert re.search(method_allow, op.method), op.op_id


def test_crs_skip_covers_every_upload_and_secret_body_route(rules: list[Any]) -> None:
    from app.api.v1.surface import OPS

    skip = regex(rules, 200)
    skipped = {op.op_id for op in OPS.values() if re.search(skip, "/api/v1" + _concrete(op.path))}
    allowed = {
        "auth_login",
        "auth_refresh",
        "auth_logout",
        "brand_documents_upload",
        "products_bulk",
        "images_upload",
        "signup_create",
        "signup_status",
    }
    assert allowed <= skipped, allowed - skipped
    # The regex is path-only and also names GET /brand/documents; the CRS match pairs it with
    # `request.method == 'POST'`, so that list call stays CRS-inspected (test_armor_rules.py).
    for rule in rules:
        if rule.waf is not None:
            assert regex(rules, rule.priority) == skip, rule.priority


# ---- decoded-path matching: encodings of an auth path are still rate limited ----


@settings(max_examples=200, deadline=None)
@given(st.data())
def test_any_percent_encoding_of_login_is_rate_limited(
    rules: list[Any], data: st.DataObject
) -> None:
    target = "/api/v1/auth/login"
    encoded = "".join(
        f"%{ord(ch):02{data.draw(st.sampled_from(['x', 'X']))}}" if data.draw(st.booleans()) else ch
        for ch in target
    )
    assert unquote(encoded) == target
    assert re.search(regex(rules, 300), unquote(encoded)), encoded


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=40))
def test_path_rule_allows_only_api_v1_and_readyz(rules: list[Any], tail: str) -> None:
    raw = "/" + tail
    allowed = re.search(regex(rules, 100), raw) is not None
    assert allowed == (raw.startswith("/api/v1/") or raw == "/readyz"), raw


# ---- locked deploy: a candidate that answers /auth/me but not /readyz never takes traffic ----


def test_locked_deploy_readyz_failure_keeps_traffic(toolkit: Path, tmp_path: Path) -> None:
    edge_ready(toolkit)
    live = run_live(
        toolkit,
        tmp_path,
        "prod",
        "deploy",
        responses=existing_service(service_doc(ingress=LOCKED, issuer=EDGE)),
        curl=[
            {"url": f"{EDGE}/api/v1/auth/me", "code": "401", "header": HEADER},
            {"url": f"{EDGE}/readyz", "body": '{"status":"down","checks":{"postgres":"down"}}'},
        ],
    )
    assert live.result.returncode != 0
    assert live.index("--to-latest") == -1
    assert live.index("update-traffic", "--remove-tags=candidate") > live.index(
        "run deploy aimos-api"
    )
    assert all(HEADER in c for c in live.curls), live.curls


def test_locked_deploy_live_unlocked_service_with_env_all_is_not_refused(
    toolkit: Path, tmp_path: Path
) -> None:
    """The guard refuses only locked -> all; all -> all on a live `all` service deploys."""
    set_env(toolkit, "prod", API_PUBLIC_URL=EDGE, JWT_ISSUER=EDGE)
    live = run_live(
        toolkit,
        tmp_path,
        "prod",
        "deploy",
        responses=existing_service(service_doc(ingress="all", issuer=EDGE)),
        curl=[
            {"url": "/api/v1/auth/me", "code": "401"},
            {"url": "/readyz", "body": '{"checks":{"postgres":"ok"}}'},
        ],
    )
    assert "live-state guard: OK" in live.out, live.out
    assert "--ingress=all" in live.calls[live.index("run deploy aimos-api")]


# ---- Bash 3.2 (macOS /bin/bash): every edge dry run, no unbound empty arrays ----


@pytest.mark.parametrize("sub", EDGE_SUBS)
def test_edge_dry_runs_bash32(toolkit: Path, tmp_path: Path, sub: str) -> None:
    version = subprocess.run(
        ["/bin/bash", "-c", "echo $BASH_VERSION"], capture_output=True, text=True, check=False
    ).stdout
    if not version.startswith("3.2"):
        pytest.skip("/bin/bash is not Bash 3.2")
    if sub == "alerts":
        set_env(toolkit, "prod", ALERT_EMAIL="ops@kuinn.in")
    if sub == "lock-ingress":
        edge_ready(toolkit)
    result = run_dry(toolkit, tmp_path, "prod", "edge", sub, interpreter="/bin/bash")
    assert result.returncode == 0, result.stderr
    assert "unbound variable" not in result.stderr
    assert gcloud_lines(result.stdout + result.stderr)
