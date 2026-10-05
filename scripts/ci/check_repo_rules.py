"""Repo rules that ruff cannot express (ADR-0013 §3). Part of `api-lint`.

- R0: a file in scope that does not parse fails closed.
- R1: no environment access (`os.environ`, `os.getenv`, ...) through any alias of `os`, and no
  `from os import` of those names, outside `apps/api/app/core/config.py`.
- R2: no `error_code=` keyword given a string literal or an f-string; codes come from
  `mkos_schemas/codes.py` only.
- R3: nothing in `apps/api/app/services/**` imports `sqlalchemy.ext.asyncio`: engine-level SQL
  from a service would bypass the tenant guard (import-linter forbids only package roots).
- R4: inside `apps/api/app/**` but outside `app/db/**` and `app/repositories/**`, no raw-SQL
  constructor (`text`, `TextClause`, `literal_column`) from any `sqlalchemy` module, whether
  imported by name or reached through an attribute chain off any `sqlalchemy` alias
  (`sa.sql.text`, `sql.text`, `expression.text`), and no `.connection(`, `.connect(`,
  `.exec_driver_sql(` or `.get_bind(` call: raw SQL and Core connections bypass the repository
  layer (layer 2 of DATABASE.md §4).
- R5: `platform_scope` (the read-only cross-tenant bypass, CANON-ADDENDUM.md A§6) is named only in
  `apps/api/app/admin/**`, `apps/api/app/cli.py`, `scripts/**`, and `apps/api/app/db/session.py`,
  which defines it. Any import form counts, including `import app.db.session as s` plus the
  attribute, and a `getattr`/`setattr`/`hasattr`/`delattr` lookup by string.
- R6: `SCOPE_OK` is named only in `apps/api/app/db/**` and `apps/api/app/repositories/base.py`. Its
  two writers are `platform_scope` (`session.info`) and `TenantScopedRepository._select()` /
  `_scoped_outer()` (the statement option); the guard in `app/db/guard.py` is its only reader.

`--rules R5,R6` limits the run to those rules (`lint-imports` runs R5 and R6; `api-lint` all).

Scope: `apps/api/app/**`, `apps/api/alembic/**`, `packages/schemas/src/**` and `scripts/**`,
less the Python 3.9 doc tooling in `scripts/docgen/**` and `scripts/trace/**`. Stdlib only.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

SCOPE = ("apps/api/app", "apps/api/alembic", "packages/schemas/src", "scripts")
EXCLUDED = ("scripts/docgen", "scripts/trace")
CONFIG_MODULE = "apps/api/app/core/config.py"
SERVICES = "apps/api/app/services"
ENV_NAMES = frozenset({"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"})
ASYNC_ENGINE_MODULE = "sqlalchemy.ext.asyncio"
APP = "apps/api/app"
RAW_SQL_ALLOWED = ("apps/api/app/db", "apps/api/app/repositories")
PLATFORM_SCOPE_ALLOWED = ("apps/api/app/admin", "scripts")
PLATFORM_SCOPE_FILES = ("apps/api/app/cli.py", "apps/api/app/db/session.py")
SCOPE_OK_ALLOWED = ("apps/api/app/db",)
SCOPE_OK_FILES = ("apps/api/app/repositories/base.py",)
SQLALCHEMY_TEXT_MODULES = frozenset({"sqlalchemy", "sqlalchemy.sql", "sqlalchemy.sql.expression"})
RAW_SQL_NAMES = frozenset({"text", "TextClause", "literal_column"})
RAW_SQL_CALLS = frozenset({"connection", "connect", "exec_driver_sql", "get_bind"})
ATTR_LOOKUPS = frozenset({"getattr", "setattr", "hasattr", "delattr"})
ALL_RULES = frozenset({"R1", "R2", "R3", "R4", "R5", "R6"})


@dataclass(frozen=True, order=True)
class Violation:
    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.message}"


def _in_scope(relative: str) -> bool:
    inside = any(relative == d or relative.startswith(d + "/") for d in SCOPE)
    excluded = any(relative == d or relative.startswith(d + "/") for d in EXCLUDED)
    return inside and not excluded


def iter_files(root: Path) -> Iterator[tuple[str, Path]]:
    for base in SCOPE:
        directory = root / base
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            if _in_scope(relative):
                yield relative, path


def _os_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os" or alias.name.startswith("os."):
                    aliases.add(alias.asname or "os")
    return aliases


def _env_access(tree: ast.AST) -> Iterator[tuple[int, str]]:
    aliases = _os_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os" and node.level == 0:
            for alias in node.names:
                if alias.name in ENV_NAMES or alias.name == "*":
                    yield node.lineno, f"`from os import {alias.name}` outside {CONFIG_MODULE}"
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in ENV_NAMES
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
        ):
            yield node.lineno, f"`{node.value.id}.{node.attr}` outside {CONFIG_MODULE}"


def _error_code_literals(tree: ast.AST) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != "error_code":
            continue
        value = node.value
        literal = isinstance(value, ast.Constant) and isinstance(value.value, str)
        if literal or isinstance(value, ast.JoinedStr):
            yield value.lineno, "`error_code=` given a string literal; use an ErrorCode member"


def _async_engine_imports(tree: ast.AST) -> Iterator[tuple[int, str]]:
    message = f"services must not import {ASYNC_ENGINE_MODULE} (go through a repository)"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == ASYNC_ENGINE_MODULE or alias.name.startswith(
                    ASYNC_ENGINE_MODULE + "."
                ):
                    yield node.lineno, message
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            module = node.module
            direct = module == ASYNC_ENGINE_MODULE or module.startswith(ASYNC_ENGINE_MODULE + ".")
            via_parent = module == "sqlalchemy.ext" and any(a.name == "asyncio" for a in node.names)
            if direct or via_parent:
                yield node.lineno, message


def _under(relative: str, dirs: tuple[str, ...]) -> bool:
    return any(relative.startswith(d + "/") for d in dirs)


def _is_sqlalchemy(module: str | None) -> bool:
    return module is not None and (module == "sqlalchemy" or module.startswith("sqlalchemy."))


def _sqlalchemy_aliases(tree: ast.AST) -> set[str]:
    """Every local name bound to a sqlalchemy module: `import sqlalchemy[.x] [as a]` and
    `from sqlalchemy[.x] import <lowercase submodule-like name> [as a]` (e.g. `sql`,
    `expression`). Lowercase non-raw names such as `select` are harmless: they have no `.text`."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_sqlalchemy(alias.name):
                    aliases.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and _is_sqlalchemy(node.module):
            for alias in node.names:
                if alias.name != "*" and alias.name.islower() and alias.name not in RAW_SQL_NAMES:
                    aliases.add(alias.asname or alias.name)
    return aliases


def _root_name(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _raw_sql(tree: ast.AST) -> Iterator[tuple[int, str]]:
    aliases = _sqlalchemy_aliases(tree)
    where = "outside app/db and app/repositories"
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and _is_sqlalchemy(node.module):
            for alias in node.names:
                star = alias.name == "*" and node.module in SQLALCHEMY_TEXT_MODULES
                if alias.name in RAW_SQL_NAMES or star:
                    yield node.lineno, f"`from {node.module} import {alias.name}` {where}"
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in RAW_SQL_NAMES
            and _root_name(node.value) in aliases
        ):
            yield node.lineno, f"`{_root_name(node.value)}...{node.attr}` {where}"
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in RAW_SQL_CALLS
        ):
            yield node.lineno, f"`.{node.func.attr}()` {where}"


def _names(tree: ast.AST, name: str, star_module: str) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == name or (alias.name == "*" and node.module == star_module):
                    yield node.lineno, f"imports `{name}`"
        elif isinstance(node, ast.Attribute) and node.attr == name:
            yield node.lineno, f"uses `{name}`"
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ATTR_LOOKUPS
            and any(isinstance(a, ast.Constant) and a.value == name for a in node.args[1:2])
        ):
            yield node.lineno, f"looks up `{name}` by string"


def check_file(relative: str, path: Path) -> list[Violation]:
    try:
        tree = ast.parse(path.read_bytes(), filename=relative)
    except (SyntaxError, ValueError) as exc:
        line = getattr(exc, "lineno", None) or 1
        return [Violation(relative, line, "R0", f"cannot parse ({type(exc).__name__})")]
    found: list[Violation] = []
    if relative != CONFIG_MODULE:
        found += [Violation(relative, n, "R1", m) for n, m in _env_access(tree)]
    found += [Violation(relative, n, "R2", m) for n, m in _error_code_literals(tree)]
    if relative.startswith(SERVICES + "/"):
        found += [Violation(relative, n, "R3", m) for n, m in _async_engine_imports(tree)]
    if relative.startswith(APP + "/") and not _under(relative, RAW_SQL_ALLOWED):
        found += [Violation(relative, n, "R4", m) for n, m in _raw_sql(tree)]
    if not (_under(relative, PLATFORM_SCOPE_ALLOWED) or relative in PLATFORM_SCOPE_FILES):
        found += [
            Violation(relative, n, "R5", f"{m} outside app/admin, app/cli.py and scripts")
            for n, m in _names(tree, "platform_scope", "app.db.session")
        ]
    if not (_under(relative, SCOPE_OK_ALLOWED) or relative in SCOPE_OK_FILES):
        found += [
            Violation(relative, n, "R6", f"{m} outside app/db and app/repositories/base.py")
            for n, m in _names(tree, "SCOPE_OK", "app.db.guard")
        ]
    return found


def find_violations(root: Path, rules: frozenset[str] | set[str] = ALL_RULES) -> list[Violation]:
    found: list[Violation] = []
    for relative, path in iter_files(root):
        found += [v for v in check_file(relative, path) if v.rule == "R0" or v.rule in rules]
    return sorted(set(found))


def _rules_arg(value: str) -> frozenset[str]:
    chosen = frozenset(part.strip() for part in value.split(",") if part.strip())
    unknown = chosen - ALL_RULES
    if not chosen or unknown:
        raise argparse.ArgumentTypeError(f"unknown rule(s): {', '.join(sorted(unknown)) or '-'}")
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the repository root")
    parser.add_argument(
        "--rules", type=_rules_arg, default=ALL_RULES, help="comma-separated, e.g. R5,R6"
    )
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()
    violations = find_violations(root, args.rules)
    for violation in violations:
        print(violation)
    checked = sum(1 for _ in iter_files(root))
    print(f"repo rules: {checked} files checked, {len(violations)} violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
