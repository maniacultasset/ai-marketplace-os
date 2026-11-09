"""Ops readiness: `deploy.sh <env> restore-drill` (the latest backup or a point in time into a
throwaway instance, checked, timed, deleted) and the cross-region backup switch (prepared, off).
Offline: gcloud, cloud-sql-proxy and psql are fakes on PATH; nothing contacts Google Cloud."""

from __future__ import annotations

import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Any

import pytest
from tests.meta.test_gcp_edge import (
    Live,
    _fake,
    gcloud_lines,
    run_dry,
    run_live,
    set_env,
    toolkit,
)

__all__ = ["toolkit"]
DEPLOY = "scripts/deploy/gcp"
DRILL = re.compile(r"aimos-db-drill-\d{8}")
REVISION = "0026_invitee_email_unverified"
RO_PASSWORD = "ro:pa\\ss-" + "r" * 20
MIGRATOR_PASSWORD = "mig-" + "m" * 30

# Answers each query of the drill; logs the user and query, and copies the pgpass file once.
FAKE_PSQL = r"""#!/usr/bin/env python3
import os, stat, sys
args = sys.argv[1:]
conn = args[0]
query = args[args.index("-c") + 1]
user = [p.split("=", 1)[1] for p in conn.split() if p.startswith("user=")][0]
with open(os.environ["PSQL_LOG"], "a", encoding="utf-8") as handle:
    handle.write("%s|%s|%s\n" % (user, os.environ.get("PGOPTIONS", ""), query))
pgpass = os.environ["PGPASSFILE"]
mode = stat.S_IMODE(os.stat(pgpass).st_mode)
copy = os.environ["PSQL_PGPASS_COPY"]
if not os.path.exists(copy):
    open(copy, "w", encoding="utf-8").write("%o\n%s" % (mode, open(pgpass, encoding="utf-8").read()))
if query == "SELECT 1":
    print(1)
elif "alembic_version" in query:
    print(os.environ.get("FAKE_REVISION", "0026_invitee_email_unverified"))
elif "FROM tenants" in query:
    print(os.environ.get("FAKE_TENANTS", "3"))
elif "pg_class" in query:
    print(12)
elif "kms_key_version" in query:
    print(os.environ.get("FAKE_UNSEALED", "0"))
elif "max(created_at)" in query:
    print("2026-11-09 10:00:00+00")
else:
    print(5)
"""
FAKE_PROXY = """#!/bin/sh
echo "$*" >> "$PROXY_LOG"
exec sleep 30
"""


@pytest.fixture
def kit(toolkit: Path, repo_root: Path) -> Path:
    """The toolkit copy plus the alembic versions the drill checks the revision against."""
    versions = toolkit.parents[2] / "apps/api/alembic/versions"
    shutil.copytree(
        repo_root / "apps/api/alembic/versions",
        versions,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return toolkit


SOURCE: dict[str, Any] = {
    "match": ["sql instances describe", "databaseVersion"],
    "stdout": "POSTGRES_17\tdb-custom-1-3840\t42\n",
}
BACKUP: dict[str, Any] = {"match": "sql backups list", "stdout": "1731100000000\n"}
LABEL: dict[str, Any] = {
    "match": ["sql instances describe", "userLabels.purpose"],
    "stdout": "restore-drill\n",
}
SECRETS: list[dict[str, Any]] = [
    {"match": "versions access latest --secret=APP_RO_PASSWORD", "stdout": RO_PASSWORD},
    {"match": "versions access latest --secret=MIGRATOR_PASSWORD", "stdout": MIGRATOR_PASSWORD},
]


def drill_live(
    kit: Path,
    tmp_path: Path,
    *args: str,
    rules: list[dict[str, Any]] | None = None,
    extra: dict[str, str] | None = None,
) -> Live:
    bin_dir = tmp_path / "bin"
    _fake(bin_dir, "psql", FAKE_PSQL)
    _fake(bin_dir, "cloud-sql-proxy", FAKE_PROXY)
    env = {
        "PSQL_LOG": str(tmp_path / "psql.log"),
        "PSQL_PGPASS_COPY": str(tmp_path / "pgpass.copy"),
        "PROXY_LOG": str(tmp_path / "proxy.log"),
        "DRILL_WAIT_SLEEP": "0",
        **(extra or {}),
    }
    default: list[dict[str, Any]] = [SOURCE, BACKUP, LABEL, *SECRETS]
    return run_live(
        kit,
        tmp_path,
        "prod",
        "restore-drill",
        *args,
        responses=default if rules is None else rules,
        extra=env,
    )


def mutating(calls: list[str]) -> list[str]:
    verbs = (" create ", " patch ", " delete ", " restore ", " clone ")
    return [c for c in calls if c.startswith("sql ") and any(v in f" {c} " for v in verbs)]


def assert_never_touches_the_source(calls: list[str]) -> None:
    """Every mutating Cloud SQL call names the drill instance as its target, never aimos-db."""
    for call in mutating(calls):
        words = shlex.split(call)
        if words[:3] == ["sql", "instances", "clone"]:
            assert words[3] == "aimos-db" and DRILL.fullmatch(words[4]), call
        elif words[:3] == ["sql", "backups", "restore"]:
            (target,) = [w for w in words if w.startswith("--restore-instance=")]
            assert DRILL.fullmatch(target.split("=", 1)[1]), call
        else:
            assert DRILL.fullmatch(words[3]), call


def psql_calls(tmp_path: Path) -> list[list[str]]:
    log = tmp_path / "psql.log"
    return [line.split("|", 2) for line in log.read_text(encoding="utf-8").splitlines()]


# ---- dry runs ----


def test_usage_lists_restore_drill(kit: Path, tmp_path: Path) -> None:
    result = run_dry(kit, tmp_path, "prod", "nope")
    assert result.returncode == 2
    assert re.search(r"^  restore-drill ", result.stderr, re.MULTILINE)
    assert result.stderr.rstrip().endswith("cors and status, ADR-0025)")


def test_dry_run_from_the_latest_backup(kit: Path, tmp_path: Path) -> None:
    result = run_dry(kit, tmp_path, "prod", "restore-drill")
    assert result.returncode == 0, result.stderr
    out = result.stdout + result.stderr
    lines = [line[len("+ gcloud ") :] for line in gcloud_lines(out)]
    assert_never_touches_the_source(lines)
    create = next(x for x in lines if x.startswith("sql instances create"))
    for flag in (
        "--no-deletion-protection",
        "--no-backup",
        "--labels=app=aimos\\,env=production\\,purpose=restore-drill",
        "--storage-size=30GB",
        "--connector-enforcement=REQUIRED",
    ):
        assert flag in create, flag
    restore = next(x for x in lines if x.startswith("sql backups restore"))
    assert "--backup-instance=aimos-db " in restore and "\\<latest-backup-id\\>" in restore
    # the mutating commands print on stdout, in the order they run
    ran = [line[len("+ gcloud ") :] for line in gcloud_lines(result.stdout)]
    order = [
        next(i for i, x in enumerate(ran) if x.startswith(p))
        for p in ("sql instances create", "sql backups restore", "sql instances delete")
    ]
    assert order == sorted(order)
    assert (
        "+ cloud-sql-proxy --gcloud-auth --port 6544 aimos-prod:asia-south1:aimos-db-drill-" in out
    )
    assert "user=migrator" in out and "FROM\\ alembic_version" in out
    assert "dry run; a real run times every phase against the 900 s gate" in out


def test_dry_run_point_in_time_clones_then_unprotects_the_clone(kit: Path, tmp_path: Path) -> None:
    result = run_dry(kit, tmp_path, "prod", "restore-drill", "--pitr", "2026-11-09T10:00:00Z")
    assert result.returncode == 0, result.stderr
    lines = [line[len("+ gcloud ") :] for line in gcloud_lines(result.stdout + result.stderr)]
    assert_never_touches_the_source(lines)
    clone = next(x for x in lines if x.startswith("sql instances clone"))
    assert "--point-in-time=2026-11-09T10:00:00Z" in clone
    patch = next(x for x in lines if x.startswith("sql instances patch"))
    assert "--no-deletion-protection" in patch and "purpose=restore-drill" in patch
    assert not [x for x in lines if x.startswith(("sql backups", "sql instances create"))]


@pytest.mark.parametrize(
    "args",
    [("--pitr", "yesterday"), ("--pitr", "2026-11-09 10:00:00"), ("--pitr",), ("--force",)],
    ids=["word", "no-z", "no-value", "unknown"],
)
def test_bad_flags_refused_before_gcloud(kit: Path, tmp_path: Path, args: tuple[str, ...]) -> None:
    live = drill_live(kit, tmp_path, *args)
    assert live.result.returncode != 0
    assert live.calls == []


# ---- live runs against the fakes ----


def test_live_drill_checks_times_and_deletes(kit: Path, tmp_path: Path) -> None:
    live = drill_live(kit, tmp_path)
    assert live.result.returncode == 0, live.out
    assert_never_touches_the_source(live.calls)
    (create,) = [c for c in live.calls if c.startswith("sql instances create")]
    name = shlex.split(create)[3]
    assert "--storage-size=42GB" in create and "--tier=db-custom-1-3840" in create
    restore = live.calls[live.index("sql backups restore 1731100000000")]
    assert f"--restore-instance={name}" in restore
    deletes = [c for c in live.calls if c.startswith("sql instances delete")]
    assert deletes == [f"sql instances delete {name} --project=aimos-prod --quiet"]
    assert live.index("sql backups restore") < live.index("sql instances delete")
    # the checks: migrator for alembic only, read-only sessions, every core table counted
    calls = psql_calls(tmp_path)
    assert all("default_transaction_read_only=on" in options for _, options, _ in calls)
    assert {user for user, _, q in calls if "alembic_version" in q} == {"migrator"}
    assert {user for user, _, q in calls if "alembic_version" not in q} == {"app_ro"}
    counted = {q.rsplit(" ", 1)[1] for _, _, q in calls if q.startswith("SELECT count(*) FROM ")}
    assert {"tenants", "products", "listings", "job_runs", "marketplace_accounts"} <= counted
    assert f"alembic revision {REVISION} (known to this checkout)" in live.out
    assert "rows tenants=3" in live.out and "smoke ok" in live.out
    assert "launch gate 6 met" in live.out
    # the proxy ran on the drill instance; the passwords went to a 0600 pgpass file only
    proxy = (tmp_path / "proxy.log").read_text(encoding="utf-8")
    assert proxy.strip() == f"--gcloud-auth --port 6544 aimos-prod:asia-south1:{name}"
    mode, pgpass = (tmp_path / "pgpass.copy").read_text(encoding="utf-8").split("\n", 1)
    assert mode == "600"
    assert pgpass.splitlines() == [
        "127.0.0.1:6544:marketplace_os:app_ro:ro\\:pa\\\\ss-" + "r" * 20,
        "127.0.0.1:6544:marketplace_os:migrator:" + MIGRATOR_PASSWORD,
    ]
    assert RO_PASSWORD not in live.out and MIGRATOR_PASSWORD not in live.out
    assert not [c for c in live.calls if RO_PASSWORD in c or MIGRATOR_PASSWORD in c]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"FAKE_REVISION": "9999_not_a_revision"}, "is not in apps/api/alembic/versions"),
        ({"FAKE_TENANTS": "0"}, "has no tenant"),
        ({"FAKE_UNSEALED": "2"}, "2 active Amazon accounts lost their KMS key version"),
    ],
    ids=["revision", "tenants", "tokens"],
)
def test_failed_check_still_deletes_the_drill(
    kit: Path, tmp_path: Path, extra: dict[str, str], message: str
) -> None:
    live = drill_live(kit, tmp_path, extra=extra)
    assert live.result.returncode != 0 and message in live.out
    assert_never_touches_the_source(live.calls)
    assert len([c for c in live.calls if c.startswith("sql instances delete")]) == 1


def test_no_backup_stops_before_creating(kit: Path, tmp_path: Path) -> None:
    live = drill_live(kit, tmp_path, rules=[SOURCE, {"match": "sql backups list", "stdout": ""}])
    assert live.result.returncode != 0 and "no successful backup" in live.out
    assert mutating(live.calls) == []


def test_missing_label_stops_before_the_restore(kit: Path, tmp_path: Path) -> None:
    live = drill_live(kit, tmp_path, rules=[SOURCE, BACKUP, *SECRETS])
    assert live.result.returncode != 0 and "no purpose=restore-drill label" in live.out
    assert live.index("sql backups restore") == -1
    assert len([c for c in live.calls if c.startswith("sql instances delete")]) == 1


def test_name_in_use_is_never_adopted(kit: Path, tmp_path: Path) -> None:
    taken = {"match": ["sql instances describe aimos-db-drill-", "value(name)"], "stdout": "x\n"}
    live = drill_live(kit, tmp_path, rules=[taken, SOURCE, BACKUP, LABEL, *SECRETS])
    assert live.result.returncode != 0 and "already exists" in live.out
    assert mutating(live.calls) == []


def test_keep_leaves_the_drill_and_prints_the_delete(kit: Path, tmp_path: Path) -> None:
    live = drill_live(kit, tmp_path, "--keep")
    assert live.result.returncode == 0, live.out
    assert live.index("sql instances delete") == -1
    assert re.search(
        r"gcloud sql instances delete aimos-db-drill-\d{8} --project=aimos-prod", live.out
    )


def test_proxy_that_never_answers_fails_and_deletes(kit: Path, tmp_path: Path) -> None:
    refuse = "#!/bin/sh\nexit 2\n"
    _fake(tmp_path / "bin", "psql", refuse)
    bin_dir = tmp_path / "bin"
    _fake(bin_dir, "cloud-sql-proxy", FAKE_PROXY)
    live = run_live(
        kit,
        tmp_path,
        "prod",
        "restore-drill",
        responses=[SOURCE, BACKUP, LABEL, *SECRETS],
        extra={
            "PROXY_LOG": str(tmp_path / "proxy.log"),
            "DRILL_WAIT_TRIES": "2",
            "DRILL_WAIT_SLEEP": "0",
        },
    )
    assert live.result.returncode != 0 and "no connection to aimos-db-drill-" in live.out
    assert len([c for c in live.calls if c.startswith("sql instances delete")]) == 1


def test_missing_tools_refused_before_gcloud(kit: Path, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    live = run_live(
        kit,
        tmp_path,
        "prod",
        "restore-drill",
        extra={"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"},
    )
    if shutil.which("cloud-sql-proxy", path="/usr/bin:/bin"):
        pytest.skip("this machine has cloud-sql-proxy in /usr/bin")
    assert live.result.returncode != 0 and "needs cloud-sql-proxy on PATH" in live.out
    assert live.calls == []


def test_every_mutating_drill_command_names_the_drill(repo_root: Path) -> None:
    """Static: in the drill section, create/patch/delete/restore name "$DRILL" after drill_guard."""
    text = (repo_root / DEPLOY / "deploy.sh").read_text(encoding="utf-8")
    body = text[text.index("# ---- restore drill") : text.index("# ---- status ----")]
    for line in body.splitlines():
        line = line.strip()
        if re.match(r"gc sql (instances (create|patch|delete)|backups restore)", line):
            assert '"$DRILL"' in line, line
            assert '"$SQL_INSTANCE"' not in line or "--backup-instance" in line, line
        if line.startswith("gc sql instances clone"):
            assert line.startswith('gc sql instances clone "$SQL_INSTANCE" "$DRILL"'), line


def test_drill_runbook(repo_root: Path) -> None:
    deploy = (repo_root / "docs/runbooks/DEPLOY-GCP.md").read_text(encoding="utf-8")
    incidents = (repo_root / "docs/runbooks/INCIDENTS.md").read_text(encoding="utf-8")
    assert "`restore-drill`" in deploy and "## Cross-region backups" in deploy
    assert "deploy.sh prod restore-drill" in incidents


# ---- cross-region backups: prepared, off ----


def test_prod_backups_stay_in_the_region_until_the_client_decides(repo_root: Path) -> None:
    text = (repo_root / DEPLOY / "prod.env").read_text(encoding="utf-8")
    assert re.search(r"^SQL_BACKUP_LOCATION=asia-south1$", text, re.MULTILINE)
    assert "asia-south2" in text


@pytest.mark.parametrize("value", ["us-central1", "asia", "eu"])
def test_backup_location_outside_india_refused_before_gcloud(
    kit: Path, tmp_path: Path, value: str
) -> None:
    set_env(kit, "prod", SQL_BACKUP_LOCATION=value)
    live = run_live(kit, tmp_path, "prod", "setup")
    assert live.result.returncode != 0
    assert "SQL_BACKUP_LOCATION must be asia-south1 or asia-south2" in live.out
    assert live.calls == []


def test_asia_south2_reaches_the_backup_flag(kit: Path, tmp_path: Path) -> None:
    """The switch, once the client says yes: the only change is the env value."""
    set_env(kit, "prod", SQL_BACKUP_LOCATION="asia-south2")
    result = run_dry(kit, tmp_path, "prod", "setup")
    assert result.returncode == 0, result.stderr
    create = next(x for x in gcloud_lines(result.stdout) if "sql instances create aimos-db " in x)
    assert "--backup-location=asia-south2" in create
