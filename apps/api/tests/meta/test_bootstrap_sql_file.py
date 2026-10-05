"""AC8: scripts/db/0000_bootstrap.sql is CANON-ADDENDUM.md §1's SQL, byte for byte (CANON.md:92);
it stays free of password literals and orders the Cloud SQL grant."""

from __future__ import annotations

import re
from pathlib import Path


def _bootstrap(repo_root: Path) -> str:
    return (repo_root / "scripts/db/0000_bootstrap.sql").read_text(encoding="utf-8")


def _addendum_sql(repo_root: Path) -> str:
    addendum = (repo_root / "docs/canon/CANON-ADDENDUM.md").read_text(encoding="utf-8")
    section = addendum[addendum.index("## 1. Bootstrap SQL") : addendum.index("## 2. Bootstrap")]
    block = re.search(r"```sql\n(.*?)```", section, flags=re.DOTALL)
    assert block is not None
    return block.group(1)


def test_file_equals_addendum_block(repo_root: Path) -> None:
    actual = (repo_root / "scripts/db/0000_bootstrap.sql").read_text(encoding="utf-8")
    assert actual == _addendum_sql(repo_root)
    assert actual.endswith(";\n")


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
    """No password or verifier literal in CI or make: CI and tests hold no secret (ADR-0013 §5)."""
    paths = [
        *sorted((repo_root / ".github/workflows").glob("*.yml")),
        repo_root / "Makefile",
        *sorted((repo_root / "scripts/ci").glob("*.sh")),
    ]
    assert len(paths) > 3
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for literal in ("PASSWORD '", 'PASSWORD "', "SCRAM-SHA-256$", "_SCRAM="):
            assert literal not in text, (path.name, literal)


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


_SCRAM_NAMES = ("APP_RW_SCRAM", "APP_RO_SCRAM", "MIGRATOR_SCRAM")


def test_password_file_is_psql_only_and_verifier_only(repo_root: Path) -> None:
    """EXECUTION-PLAN.md:545: only pre-computed SCRAM verifiers, checked before anything is sent."""
    text = (repo_root / "scripts/db/0000_role_passwords.sql").read_text(encoding="utf-8")
    lines = text.splitlines()
    alters = [line for line in lines if re.search(r"\bPASSWORD\b", line) and "ALTER ROLE" in line]
    assert alters == [
        "ALTER ROLE app_rw PASSWORD :'app_rw_scram';",
        "ALTER ROLE app_ro PASSWORD :'app_ro_scram';",
        "ALTER ROLE migrator PASSWORD :'migrator_scram';",
    ]
    getenvs = re.findall(r"^\\getenv (\w+) (\w+)$", text, re.MULTILINE)
    assert getenvs == [(name.lower(), name) for name in _SCRAM_NAMES]
    assert "_PASSWORD" not in text
    assert not re.search(r"PASSWORD\s+['\"]", text)
    shell = [line for line in lines if line.startswith("\\set ") and "`" in line]
    assert shell
    for line in shell:
        assert ":'" not in line
        assert re.search(r'"\$\{[A-Z_]+_SCRAM-\}"', line), line
    check = next(i for i, line in enumerate(lines) if line.startswith("\\set scram_ok `"))
    pw_safe = lines.index("\\if :pw_safe")
    first_getenv = next(i for i, line in enumerate(lines) if line.startswith("\\getenv"))
    assert check < pw_safe < first_getenv
    query = "\n".join(lines[check:pw_safe])
    for setting in (
        "'log_statement'",
        "'log_min_duration_statement'",
        "'log_min_duration_sample'",
        "'pgaudit.log'",
    ):
        assert setting in query, setting


def test_makefile_passes_scram_vars_by_name(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    body = re.search(r"^bootstrap-db:\n((?:\t.+\n)+)", makefile, re.MULTILINE)
    assert body is not None
    recipe = body.group(1)
    assert "-e APP_RW_SCRAM -e APP_RO_SCRAM -e MIGRATOR_SCRAM postgres" in recipe
    assert not re.search(r"_SCRAM=", recipe)
    assert "-f - < scripts/db/0000_bootstrap.sql" in recipe
    assert "-f - < scripts/db/0000_role_passwords.sql" in recipe
    assert "/docker-entrypoint-initdb.d/" not in makefile
    test_db = re.search(r"^test-db:\n((?:\t.+\n)+)", makefile, re.MULTILINE)
    assert test_db is not None
    assert "-f - < scripts/db/0000_bootstrap.sql" in test_db.group(1)
    assert "role_passwords" not in test_db.group(1)
