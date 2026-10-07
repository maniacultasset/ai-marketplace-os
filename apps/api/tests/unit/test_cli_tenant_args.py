"""S16 T19-T20a (DW4, AC8): the staff onboarding CLI's arguments, refused before any connection."""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest
from app.cli import build_parser, main

CREATE = [
    "tenant",
    "create",
    "--name",
    "Acme",
    "--slug",
    "acme-co",
    "--owner-email",
    "o@acme.test",
    "--staff-email",
    "s@ops.test",
]
CLI = Path(__file__).resolve().parents[2] / "app" / "cli.py"


def _exit(argv: list[str], capsys: pytest.CaptureFixture[str]) -> int:
    with pytest.raises(SystemExit) as info:
        main(argv)
    capsys.readouterr()
    return int(info.value.code or 0)


@pytest.mark.parametrize(
    "case",
    [
        "bad_slug",
        "upper_slug",
        "blank_name",
        "long_name",
        "bad_email",
        "two_at",
        "feff_email",
        "nbsp_email",
        "unicode_email",
        "trial_0",
        "trial_366",
        "missing_staff",
    ],
)
def test_cli_tenant_create_args_exit_2(case: str, capsys: pytest.CaptureFixture[str]) -> None:
    argv = list(CREATE)
    edits = {
        "bad_slug": ("--slug", "-acme"),
        "upper_slug": ("--slug", "Acme"),
        "blank_name": ("--name", "   "),
        "long_name": ("--name", "x" * 201),
        "bad_email": ("--owner-email", "no-at-sign"),
        "two_at": ("--owner-email", "a@b@c.test"),
        "feff_email": ("--owner-email", "o" + chr(0xFEFF) + "@acme.test"),
        "nbsp_email": ("--owner-email", "o" + chr(0xA0) + "x@acme.test"),
        "unicode_email": ("--owner-email", "ówner@acme.test"),
    }
    if case in edits:
        flag, value = edits[case]
        argv[argv.index(flag) + 1] = value
    elif case.startswith("trial_"):
        argv += ["--trial-days", case.split("_", 1)[1]]
    else:
        index = argv.index("--staff-email")
        del argv[index : index + 2]
    assert _exit(argv, capsys) == 2


def test_cli_email_trimmed_and_lowercased() -> None:
    argv = list(CREATE)
    argv[argv.index("--owner-email") + 1] = "  Owner@ACME.test "
    args = build_parser().parse_args(argv)
    assert args.owner_email == "owner@acme.test" and args.trial_days is None


def test_cli_email_longer_than_254_octets_refused(capsys: pytest.CaptureFixture[str]) -> None:
    argv = list(CREATE)
    argv[argv.index("--owner-email") + 1] = "a" * 250 + "@b.test"
    assert _exit(argv, capsys) == 2


@pytest.mark.parametrize("case", ["bad_role", "missing_staff", "bad_slug"])
def test_cli_add_member_args_exit_2(case: str, capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "tenant",
        "add-member",
        "--slug",
        "acme",
        "--email",
        "m@acme.test",
        "--role",
        "reviewer",
        "--staff-email",
        "s@ops.test",
    ]
    if case == "bad_role":
        argv[argv.index("--role") + 1] = "superuser"
    elif case == "bad_slug":
        argv[argv.index("--slug") + 1] = "x"
    else:
        argv = argv[:-2]
    assert _exit(argv, capsys) == 2


@pytest.mark.parametrize("case", ["bad_email", "missing_staff"])
def test_cli_set_password_args_exit_2(case: str, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["user", "set-password", "--email", "m@acme.test", "--staff-email", "s@ops.test"]
    if case == "bad_email":
        argv[argv.index("--email") + 1] = "m" + chr(0xFEFF) + "@acme.test"
    else:
        argv = argv[:-2]
    assert _exit(argv, capsys) == 2


def test_password_never_in_argv() -> None:
    parser = build_parser()
    for group in ("tenant", "user"):
        for argv in (
            ["tenant", "create", "--help"],
            ["tenant", "add-member", "--help"],
            ["user", "set-password", "--help"],
        ):
            if argv[0] != group:
                continue
            text = io.StringIO()
            with pytest.raises(SystemExit):
                import contextlib

                with contextlib.redirect_stdout(text):
                    parser.parse_args(argv)
            assert (
                "password" not in text.getvalue().split("usage:", 1)[1].split("options:")[1].lower()
            )


def test_read_password_bounds() -> None:
    from app.core.passwords import read_password

    assert read_password(io.StringIO("x" * 12 + "\nignored\n")).get_secret_value() == "x" * 12
    assert read_password(io.StringIO("y" * 1024 + "\r\n")).get_secret_value() == "y" * 1024
    for bad in ("", "short\n", "z" * 1025 + "\n"):
        with pytest.raises(ValueError) as info:
            read_password(io.StringIO(bad))
        assert "short" not in str(info.value) and "z" * 20 not in str(info.value)


def test_cli_tenant_has_no_sql() -> None:
    tree = ast.parse(CLI.read_text(encoding="utf-8"))
    strings = [
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    sql = [
        s for s in strings if s.lstrip().startswith(("INSERT ", "UPDATE ", "SELECT ", "DELETE "))
    ]
    assert sql == []
