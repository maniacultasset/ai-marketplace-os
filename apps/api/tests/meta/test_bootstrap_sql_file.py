"""The bootstrap file stays free of password literals and orders the Cloud SQL grant."""

from __future__ import annotations

import re
from pathlib import Path


def _bootstrap(repo_root: Path) -> str:
    return (repo_root / "scripts/db/0000_bootstrap.sql").read_text(encoding="utf-8")


def test_bootstrap_sql_has_no_password_literal(repo_root: Path) -> None:
    text = (repo_root / "scripts/db/0000_bootstrap.sql").read_text(encoding="utf-8")
    assert "PASSWORD" not in text
    assert ":'" not in text


def test_grant_migrator_precedes_owner_change(repo_root: Path) -> None:
    text = (repo_root / "scripts/db/0000_bootstrap.sql").read_text(encoding="utf-8")
    grant = text.index("GRANT migrator TO")
    owner = text.index("ALTER SCHEMA public OWNER TO migrator")
    assert grant < owner


def test_workflows_have_no_password_literals(repo_root: Path) -> None:
    text = (repo_root / ".github/workflows/ci-api.yml").read_text(encoding="utf-8")
    assert "PASSWORD '" not in text
    assert 'PASSWORD "' not in text


def _statements(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.lstrip().startswith("--")]


def test_default_privileges_run_as_migrator(repo_root: Path) -> None:
    """A non-superuser holds migrator WITH INHERIT FALSE (DATABASE.md:1752), so the schema grant
    and the default privileges run under SET ROLE migrator."""
    text = "\n".join(_statements(_bootstrap(repo_root)))
    owner = text.index("ALTER SCHEMA public OWNER TO migrator")
    set_role = text.index("SET ROLE migrator;")
    usage = text.index("GRANT USAGE ON SCHEMA public TO")
    reset = text.index("RESET ROLE;")
    defaults = [m.start() for m in re.finditer(r"ALTER DEFAULT PRIVILEGES", text)]
    assert len(defaults) == 3
    assert owner < set_role < usage < min(defaults)
    assert max(defaults) < reset
    assert text.count("SET ROLE migrator;") == 1
    assert text.count("RESET ROLE;") == 1


def test_execute_revoke_is_global(repo_root: Path) -> None:
    """The schema-scoped form leaves PUBLIC EXECUTE in place (DATABASE.md:1752)."""
    text = " ".join(" ".join(_statements(_bootstrap(repo_root))).split())
    statements = [s.strip() for s in text.split(";")]
    revoke = [s for s in statements if "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC" in s]
    assert revoke == [
        "ALTER DEFAULT PRIVILEGES FOR ROLE migrator REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
    ]


def test_mimic_suite_is_marked_and_split(repo_root: Path) -> None:
    """AC 5 is proved by `make bootstrap-mimic` in the CI `bootstrap` job, never by api-tests."""
    pyproject = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^\s*"cloudsql_mimic: ', pyproject, re.MULTILINE)
    module = repo_root / "apps/api/tests/cloudsql_mimic/test_bootstrap_mimic.py"
    assert "pytest.mark.cloudsql_mimic" in module.read_text(encoding="utf-8")
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    api_tests = re.search(r"^api-tests:\n((?:\t.+\n)+)", makefile, re.MULTILINE)
    assert api_tests is not None
    assert '-m "not tenant_isolation and not cloudsql_mimic"' in api_tests.group(1)
    mimic = re.search(r"^bootstrap-mimic:\n((?:\t.+\n)+)", makefile, re.MULTILINE)
    assert mimic is not None
    assert mimic.group(1).strip() == (
        "uv run --frozen pytest -q -m cloudsql_mimic --no-cov apps/api/tests/cloudsql_mimic"
    )
    lines = (repo_root / "scripts/ci/bootstrap.sh").read_text(encoding="utf-8").splitlines()
    check_image = next(
        i for i, line in enumerate(lines) if line.startswith("scripts/ci/check_image.sh")
    )
    assert "make bootstrap-mimic" in lines[check_image + 1 :]
    prove = (repo_root / "scripts/ci/prove_gates.sh").read_text(encoding="utf-8")
    assert re.search(
        r"^\s*leg bootstrap-mimic-no-set-role bootstrap 'permission denied to change default "
        r"privileges' seed_bootstrap_no_set_role ",
        prove,
        re.MULTILINE,
    )
    assert "seed_bootstrap_no_set_role() {" in prove
