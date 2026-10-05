"""The Alembic environment files: alembic.ini, env.py, the revision template and versions/
(CANON.md:20 sync psycopg; CANON.md:75-76; DATABASE.md:1813)."""

from __future__ import annotations

import ast
import configparser
import fnmatch
import re
from pathlib import Path

import pytest

API = Path(__file__).resolve().parents[2]
ALEMBIC_DIR = API / "alembic"
ENV_PY = ALEMBIC_DIR / "env.py"
LOCK_TIMEOUT_STMT = "SET lock_timeout = '5s'"
LEDGER_ROW = re.compile(r"^\| `([0-9]{4}[a-z]?_[a-z0-9_]+)` \|", re.MULTILINE)
CONFIGURE_FLAGS = ("compare_type", "compare_server_default", "transaction_per_migration")


def _env_tree() -> ast.Module:
    return ast.parse(ENV_PY.read_text(encoding="utf-8"))


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"env.py has no function {name}")


def _calls_in_order(node: ast.AST) -> list[ast.Call]:
    calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
    return sorted(calls, key=lambda c: (c.lineno, c.col_offset))


def test_alembic_config_ini_keys() -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(API / "alembic.ini", encoding="utf-8")
    assert parser.sections() == ["alembic"]
    section = parser["alembic"]
    assert section["script_location"] == "%(here)s/alembic"
    assert section["path_separator"] == "os"
    assert section["file_template"] == "%%(rev)s_%%(slug)s"
    assert section["truncate_slug_length"] == "40"
    assert "sqlalchemy.url" not in section
    assert "prepend_sys_path" not in section
    assert not parser.has_section("loggers")


def test_alembic_config_env_reads_settings_only() -> None:
    tree = _env_tree()
    imported: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            names.update(alias.name for alias in node.names)
    assert "get_settings" in names
    assert "os" not in imported
    assert not any(module.startswith("os.") for module in imported)
    calls = {_call_name(n) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert "fileConfig" not in calls
    assert "fileConfig" not in names


def test_alembic_config_configure_flags() -> None:
    configures = [
        n
        for n in ast.walk(_env_tree())
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "configure"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "context"
    ]
    assert len(configures) == 2, "one offline and one online context.configure"
    for call in configures:
        keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        for flag in CONFIGURE_FLAGS:
            value = keywords.get(flag)
            assert isinstance(value, ast.Constant) and value.value is True, flag
        render = keywords.get("render_item")
        assert isinstance(render, ast.Name) and render.id == "render_item"
        target = keywords.get("target_metadata")
        assert isinstance(target, ast.Name) and target.id == "target_metadata"


def test_alembic_config_online_order() -> None:
    tree = _env_tree()
    online = [_call_name(c) for c in _calls_in_order(_function(tree, "run_migrations_online"))]
    wanted = [
        "assume_migration_role",
        "acquire_migration_lock",
        "refuse_if_ahead",
        "commit",
        "configure",
        "run_migrations",
        "revoke_version_table_privileges",
        "commit",
    ]
    positions: list[int] = []
    start = 0
    for name in wanted:
        assert name in online[start:], f"{name} missing or out of order in {online}"
        index = online.index(name, start)
        positions.append(index)
        start = index + 1
    assert positions == sorted(positions)
    # A failed revision still revokes: the handler around run_migrations rolls back, revokes,
    # commits, and re-raises the original error.
    (attempt,) = [
        n
        for n in ast.walk(_function(tree, "run_migrations_online"))
        if isinstance(n, ast.Try)
        and n.handlers
        and "run_migrations" in {_call_name(c) for c in _calls_in_order(ast.Module(n.body, []))}
    ]
    (handler,) = attempt.handlers
    handled = [_call_name(c) for c in _calls_in_order(ast.Module(handler.body, []))]
    assert handled[:3] == ["rollback", "revoke_version_table_privileges", "commit"], handled
    last = handler.body[-1]
    assert isinstance(last, ast.Raise) and last.exc is None
    offline = {_call_name(c) for c in _calls_in_order(_function(tree, "run_migrations_offline"))}
    assert "assume_migration_role" not in offline
    assert "revoke_version_table_privileges" not in offline


def test_alembic_config_template_sets_lock_timeout_first(tmp_path: Path) -> None:
    from alembic import command
    from app.db.migrations import alembic_config

    cfg = alembic_config()
    cfg.set_main_option("version_locations", str(tmp_path))
    command.revision(cfg, message="probe", rev_id="t1", version_path=str(tmp_path))
    (written,) = tmp_path.glob("t1_*.py")
    source = written.read_text(encoding="utf-8")
    compile(source, str(written), "exec")
    tree = ast.parse(source)
    for name in ("upgrade", "downgrade"):
        body = _function(tree, name).body
        first = (
            body[1]
            if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            else body[0]
        )
        assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call), name
        call = first.value
        assert ast.unparse(call.func) == "op.execute", name
        assert [ast.literal_eval(a) for a in call.args] == [LOCK_TIMEOUT_STMT], name


def _ledger_ids(repo_root: Path) -> set[str]:
    canon = (repo_root / "docs/canon/CANON.md").read_text(encoding="utf-8")
    return set(LEDGER_ROW.findall(canon))


def test_alembic_config_versions_hold_only_ledger_ids(repo_root: Path) -> None:
    """Every revision file is named after its id, and the id is claimed in the Canon ledger
    before the file is written (CANON.md, migration ledger)."""
    ledger = _ledger_ids(repo_root)
    assert "0001_core_tenancy" in ledger
    versions = ALEMBIC_DIR / "versions"
    assert versions.is_dir()
    names = sorted(p.name for p in versions.iterdir() if p.name != "__pycache__")
    assert ".gitkeep" in names
    for name in names:
        if name == ".gitkeep":
            continue
        path = versions / name
        assert path.suffix == ".py", name
        assert path.stem in ledger, f"{name} is not a Canon ledger id"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assigned = {
            target.id: node.value
            for node in tree.body
            if isinstance(node, ast.Assign | ast.AnnAssign)
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
        }
        revision = assigned.get("revision")
        assert isinstance(revision, ast.Constant) and revision.value == path.stem, name


def test_env_imports_models() -> None:
    """env.py registers the models, and a caller may swap the compared MetaData through
    `config.attributes` (scratch-revision tests compare against their own)."""
    tree = _env_tree()
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "app.db.models" in imports
    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and ast.unparse(node.func.value) == "context.config.attributes"
    ]
    assert len(reads) == 1
    (read,) = reads
    assert [ast.unparse(arg) for arg in read.args] == ["'target_metadata'", "metadata"]


def _dockerignore_patterns(repo_root: Path) -> list[str]:
    lines = (repo_root / ".dockerignore").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


@pytest.mark.parametrize(
    "path",
    ["apps/api/alembic.ini", "apps/api/alembic/env.py", "apps/api/alembic/script.py.mako"],
)
def test_alembic_config_in_image(repo_root: Path, path: str) -> None:
    dockerfile = (repo_root / "apps/api/Dockerfile").read_text(encoding="utf-8")
    assert "COPY apps/api/ apps/api/" in dockerfile
    for pattern in _dockerignore_patterns(repo_root):
        if pattern.startswith("!"):
            continue
        bare = pattern.rstrip("/")
        prefix = bare.removeprefix("**/")
        parts = Path(path).parts
        hits = fnmatch.fnmatch(path, bare) or any(
            fnmatch.fnmatch("/".join(parts[i:j]), prefix)
            for i in range(len(parts))
            for j in range(i + 1, len(parts) + 1)
            if pattern.startswith("**/") or i == 0
        )
        assert not hits, f".dockerignore {pattern!r} excludes {path}"
