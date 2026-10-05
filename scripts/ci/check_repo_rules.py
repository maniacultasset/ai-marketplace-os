"""Repo rules that ruff cannot express (ADR-0013 §3). Part of `api-lint`.

- R0: a file in scope that does not parse fails closed.
- R1: no environment access (`os.environ`, `os.getenv`, ...) through any alias of `os`, and no
  `from os import` of those names, outside `apps/api/app/core/config.py`.
- R2: no `error_code=` keyword given a string literal or an f-string; codes come from
  `mkos_schemas/codes.py` only.
- R3: nothing in `apps/api/app/services/**` imports `sqlalchemy.ext.asyncio`: engine-level SQL
  from a service would bypass the tenant guard (import-linter forbids only package roots).

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
    return found


def find_violations(root: Path) -> list[Violation]:
    found: list[Violation] = []
    for relative, path in iter_files(root):
        found += check_file(relative, path)
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the repository root")
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()
    violations = find_violations(root)
    for violation in violations:
        print(violation)
    checked = sum(1 for _ in iter_files(root))
    print(f"repo rules: {checked} files checked, {len(violations)} violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
