"""DW2 T9: argument checks of the PTD commands (argparse exits 2 before any setting or call)."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.cli import main

STAFF = ["--staff-email", "staff@example.com", "--reason", "DW2 refresh"]


def _exit(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as caught:
        main(argv)
    return int(caught.value.code or 0)


@pytest.mark.parametrize(
    "argv",
    [
        ["templates", "refresh", "--product-type", "KURTA", "--reason", "r"],
        ["templates", "refresh", "--product-type", "KURTA", "--staff-email", "s@example.com"],
        [
            "templates",
            "promote-definition",
            "--product-type",
            "KURTA",
            "--version",
            "U1",
            "--reason",
            "r",
        ],
        [
            "templates",
            "promote-definition",
            "--product-type",
            "KURTA",
            "--version",
            "U1",
            "--staff-email",
            "s@example.com",
        ],
    ],
)
def test_staff_and_reason_required(argv: list[str]) -> None:
    assert _exit(argv) == 2


@pytest.mark.parametrize("product_type", ["kurta", "A/B", "A" * 65, "", " ", "KURTA?x", "KÜRTA"])
@pytest.mark.parametrize("command", ["refresh", "promote-definition", "diff"])
def test_bad_product_type(command: str, product_type: str) -> None:
    extra = {"refresh": STAFF, "promote-definition": ["--version", "U1", *STAFF], "diff": []}
    assert _exit(["templates", command, "--product-type", product_type, *extra[command]]) == 2


def test_save_schema_needs_dry_run(tmp_path: Path) -> None:
    argv = ["templates", "refresh", "--product-type", "KURTA", *STAFF]
    assert _exit([*argv, "--save-schema", str(tmp_path / "k.json")]) == 2
    assert not (tmp_path / "k.json").exists()


def test_diff_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["templates", "diff", "--product-type", "KURTA", "--from-file", str(tmp_path / "x")]
    )
    assert code == 2 and "no such file" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["refresh", "promote-definition", "diff"])
def test_requirements_choices(command: str) -> None:
    extra = {"refresh": STAFF, "promote-definition": ["--version", "U1", *STAFF], "diff": []}
    argv = ["templates", command, "--product-type", "KURTA", "--requirements", "OFFER"]
    assert _exit([*argv, *extra[command]]) == 2


@pytest.mark.parametrize("bad", ["--version=a b", "--digest=xyz", "--digest=ab"])
def test_promote_version_and_digest_checked(bad: str) -> None:
    argv = ["templates", "promote-definition", "--product-type", "KURTA", *STAFF]
    if not bad.startswith("--version"):
        argv += ["--version", "U1"]
    assert _exit([*argv, bad]) == 2


def test_templates_load_still_parses() -> None:
    from app.cli import PLATFORM_TEMPLATES_PATH, build_parser

    args = build_parser().parse_args(["templates", "load"])
    assert args.command == "load" and args.file == PLATFORM_TEMPLATES_PATH


def test_diff_from_file_without_a_database(
    clean_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.support.fake_marketplace import ptd_fixture

    good = clean_env / "KURTA.json"
    good.write_bytes(ptd_fixture("KURTA.schema.json"))
    assert main(["templates", "diff", "--product-type", "KURTA", "--from-file", str(good)]) == 0
    drift = clean_env / "drift.json"
    drift.write_bytes(ptd_fixture("KURTA.drift.schema.json"))
    capsys.readouterr()
    assert main(["templates", "diff", "--product-type", "KURTA", "--from-file", str(drift)]) == 1
    out = capsys.readouterr().out
    assert "neck_style" in out and "brand" in out
    bad = clean_env / "bad.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    assert main(["templates", "diff", "--product-type", "KURTA", "--from-file", str(bad)]) == 3
