"""app.cli `db migrate` (EXECUTION-PLAN.md:300-305): one fixed output line per outcome, exit codes
0/1/3/4, and never a DSN, a user, a host or a driver message (`command.upgrade` is faked)."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from app import cli
from app.db.migrations import ALEMBIC_INI, DatabaseAheadError, MigrationLockBusy
from sqlalchemy.exc import OperationalError

CLI_PY = Path(cli.__file__)
LEAKS = ("leakuser_s5", "leakpw_s5", "127.0.0.1", "@", "postgresql")


class _DriverError(Exception):
    def __init__(self, sqlstate: object, message: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


def _operational(sqlstate: object) -> OperationalError:
    orig = _DriverError(
        sqlstate,
        'connection to server at "127.0.0.1", port 5432 failed: FATAL: password authentication '
        'failed for user "leakuser_s5" (postgresql://leakuser_s5:leakpw_s5@127.0.0.1:5432/x)',
    )
    return OperationalError("SELECT 1", {}, orig)


def _fake_upgrade(monkeypatch: pytest.MonkeyPatch, error: BaseException | None) -> list[Any]:
    calls: list[Any] = []

    def upgrade(config: Config, revision: str) -> None:
        calls.append((config, revision))
        if error is not None:
            raise error

    monkeypatch.setattr("app.cli.command.upgrade", upgrade)
    return calls


def _no_leak(*streams: str) -> None:
    for stream in streams:
        for token in LEAKS:
            assert token not in stream, token


def test_cli_db_migrate_calls_run_migrate_with_repo_config(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Config] = []

    def run_migrate(config: Config) -> int:
        seen.append(config)
        return 7

    monkeypatch.setattr(cli, "run_migrate", run_migrate)
    assert cli.main(["db", "migrate"]) == 7
    (config,) = seen
    assert config.config_file_name == str(ALEMBIC_INI)


@pytest.mark.parametrize("argv", [[], ["db"], ["seed"], ["db", "upgrade"]])
def test_cli_requires_a_command(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(argv)
    assert info.value.code == 2
    capsys.readouterr()


def test_migrate_success_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls = _fake_upgrade(monkeypatch, None)
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("version_locations", str(tmp_path))
    assert cli.run_migrate(config) == 0
    assert calls == [(config, "head")]
    out, err = capsys.readouterr()
    assert out == "migrate: ok (head none)\n"
    assert err == ""


def test_migrate_success_line_names_the_script_head(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_upgrade(monkeypatch, None)
    config = Config(str(ALEMBIC_INI))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads, "versions/ holds at least revision 0001"
    assert cli.run_migrate(config) == 0
    out, _ = capsys.readouterr()
    assert out == f"migrate: ok (head {','.join(sorted(heads))})\n"


def test_migrate_database_ahead_exit_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_upgrade(monkeypatch, DatabaseAheadError("ffffffffffff"))
    assert cli.run_migrate(Config(str(ALEMBIC_INI))) == 3
    out, err = capsys.readouterr()
    assert err == (
        "migrate: refused: the database is at revision ffffffffffff, "
        "which this image does not contain\n"
    )
    assert out == ""


def test_migrate_database_ahead_hostile_revision_is_masked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_upgrade(monkeypatch, DatabaseAheadError("x\n@leakpw_s5"))
    assert cli.run_migrate(Config(str(ALEMBIC_INI))) == 3
    _, err = capsys.readouterr()
    assert "<invalid revision id>" in err
    _no_leak(err)


def test_migrate_lock_busy_exit_4(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_upgrade(monkeypatch, MigrationLockBusy())
    assert cli.run_migrate(Config(str(ALEMBIC_INI))) == 4
    out, err = capsys.readouterr()
    assert err == "migrate: another migration holds the lock; nothing was applied\n"
    assert out == ""


def test_migrate_other_error_class_and_sqlstate_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_upgrade(monkeypatch, _operational("28P01"))
    assert cli.run_migrate(Config(str(ALEMBIC_INI))) == 1
    out, err = capsys.readouterr()
    assert err == "migrate: failed (OperationalError, SQLSTATE 28P01)\n"
    _no_leak(out, err)


@pytest.mark.parametrize("sqlstate", ["x\n@y", "28p01", "2", None, 12345])
def test_migrate_bad_sqlstate_not_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], sqlstate: object
) -> None:
    _fake_upgrade(monkeypatch, _operational(sqlstate))
    assert cli.run_migrate(Config(str(ALEMBIC_INI))) == 1
    _, err = capsys.readouterr()
    assert err == "migrate: failed (OperationalError)\n"


def test_migrate_runtime_error_without_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_upgrade(monkeypatch, RuntimeError("ALEMBIC_DATABASE_URL is required to run migrations"))
    assert cli.run_migrate(Config(str(ALEMBIC_INI))) == 1
    _, err = capsys.readouterr()
    assert err == "migrate: failed (RuntimeError)\n"


def test_migrate_lock_timeout_inside_revision_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A 55P03 from a revision's DDL is not the migration lock: earlier revisions may be applied.
    _fake_upgrade(monkeypatch, _operational("55P03"))
    assert cli.run_migrate(Config(str(ALEMBIC_INI))) == 1
    _, err = capsys.readouterr()
    assert err == "migrate: failed (OperationalError, SQLSTATE 55P03)\n"
    assert "nothing was applied" not in err


def test_migrate_settings_boot_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_upgrade(
        monkeypatch, SystemExit("settings: APP_ROLE=migrate requires ALEMBIC_DATABASE_URL")
    )
    with pytest.raises(SystemExit):
        cli.run_migrate(Config(str(ALEMBIC_INI)))


def test_main_block_logs_to_stdout() -> None:
    tree = ast.parse(CLI_PY.read_text(encoding="utf-8"))
    (block,) = [
        node
        for node in tree.body
        if isinstance(node, ast.If) and "__main__" in ast.unparse(node.test)
    ]
    calls = [
        n
        for n in ast.walk(block)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "logging.basicConfig"
    ]
    assert len(calls) == 1
    keywords = {kw.arg: ast.unparse(kw.value) for kw in calls[0].keywords}
    assert keywords["stream"] == "sys.stdout"
    assert keywords["level"] == "logging.INFO"
    everywhere = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "logging.basicConfig"
    ]
    assert everywhere == calls, "logging is configured only in the __main__ block"


# ---- the migrate job's loaders take no path: their defaults ship in the image ----


@pytest.mark.parametrize(
    ("group", "runner", "default"),
    [
        ("rules", "run_rules_load", "RULES_SEED_PATH"),
        ("templates", "run_templates_load", "PLATFORM_TEMPLATES_PATH"),
    ],
)
def test_loader_file_defaults(
    monkeypatch: pytest.MonkeyPatch, group: str, runner: str, default: str
) -> None:
    seen: list[Path] = []

    async def fake(path: Path) -> int:
        seen.append(path)
        return 0

    monkeypatch.setattr(cli, runner, fake)
    assert cli.main([group, "load"]) == 0
    assert seen == [getattr(cli, default)]
    assert getattr(cli, default).is_file()


def test_rules_load_missing_file_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["rules", "load", "--file", str(tmp_path / "absent.json")]) == 2
    assert capsys.readouterr().err == "rules load: no such file\n"


# ---- DPP-1 (ADR-0021): user unlock, user reset-mfa and the password rules ----

UNLOCK = ["user", "unlock", "--email", "m@acme.test", "--staff-email", "s@ops.test"]
RESET = ["user", "reset-mfa", "--email", "m@acme.test", "--staff-email", "s@ops.test"]


def test_user_unlock_args(capsys: pytest.CaptureFixture[str]) -> None:
    args = cli.build_parser().parse_args(UNLOCK)
    assert (args.group, args.command, args.email, args.staff_email) == (
        "user",
        "unlock",
        "m@acme.test",
        "s@ops.test",
    )
    with pytest.raises(SystemExit) as info:
        cli.main(UNLOCK[:-2])
    assert info.value.code == 2


def test_user_reset_mfa_args(capsys: pytest.CaptureFixture[str]) -> None:
    args = cli.build_parser().parse_args(RESET)
    assert (args.command, args.email) == ("reset-mfa", "m@acme.test")
    with pytest.raises(SystemExit) as info:
        cli.main([*RESET[:3], "m" + chr(0xFEFF) + "@acme.test", *RESET[4:]])
    assert info.value.code == 2


def test_reset_mfa_password_never_from_argv(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.build_parser().parse_args([*RESET, "--password", "Long-enough-Pass-0123"])
    assert info.value.code == 2


class _Engine:
    async def dispose(self) -> None:
        return None


@pytest.mark.parametrize("argv", [RESET, ["user", "set-password", *RESET[2:]]])
async def test_policy_error_exit_2_no_password_in_stderr(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    from app.core import password_policy
    from app.db import staff_ops

    weak = "mmmmmmmmmmmmmm-weak"

    async def refuse(*_: Any, password: Any, **__: Any) -> Any:
        plain = password.get_secret_value()
        raise password_policy.PolicyError(
            password_policy.check(plain, email="m@acme.test", display_name=None)
        )

    monkeypatch.setattr(cli, "_admin_engine", lambda: _Engine())
    monkeypatch.setattr(staff_ops, "reset_mfa", refuse)
    monkeypatch.setattr(staff_ops, "set_password", refuse)
    code = await cli.run_tenant_command(
        cli.build_parser().parse_args(argv), io.StringIO(weak + "\n")
    )
    out = capsys.readouterr()
    assert code == 2
    assert weak not in out.err and weak not in out.out
    assert "upper-case" in out.err


async def test_new_user_password_checked_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    from app.db import staff_ops

    async def no_user(*_: Any) -> bool:
        return False

    created: list[Any] = []

    async def create(*_: Any, **kw: Any) -> Any:
        created.append(kw)
        raise AssertionError("no write for a weak password")

    monkeypatch.setattr(cli, "_admin_engine", lambda: _Engine())
    monkeypatch.setattr(staff_ops, "user_exists", no_user)
    monkeypatch.setattr(staff_ops, "create_tenant_with_owner", create)
    argv = [
        "tenant", "create", "--name", "Acme", "--slug", "acme-co",
        "--owner-email", "owner@acme.test", "--staff-email", "s@ops.test",
    ]  # fmt: skip
    code = await cli.run_tenant_command(
        cli.build_parser().parse_args(argv), io.StringIO("owner-password-01\n")
    )
    assert code == 2 and created == []
    assert "owner-password-01" not in capsys.readouterr().err


async def test_unlock_reads_no_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io
    import uuid

    from app.db import staff_ops

    found = uuid.uuid4()

    async def unlock(*_: Any, **kw: Any) -> uuid.UUID:
        assert set(kw) == {"email", "staff_email", "request_id"}
        return found

    monkeypatch.setattr(cli, "_admin_engine", lambda: _Engine())
    monkeypatch.setattr(staff_ops, "unlock_user", unlock)
    stdin = io.StringIO("never-read\n")
    code = await cli.run_tenant_command(cli.build_parser().parse_args(UNLOCK), stdin)
    assert code == 0 and stdin.read() == "never-read\n"
    assert capsys.readouterr().out == f"user_id={found} unlocked=true\n"
