import ast
from pathlib import Path

FORBIDDEN = {"app", "sqlalchemy", "fastapi", "starlette", "psycopg", "procrastinate"}
ROOT = Path(__file__).resolve().parents[1] / "src" / "mkos_pipeline"


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_package_does_not_import_backend_stack() -> None:
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = _imported_names(tree) & FORBIDDEN
        if found:
            offenders.append(f"{path.relative_to(ROOT)}: {sorted(found)}")
    assert offenders == []


def test_fastapi_import_inside_package_would_fail_this_test() -> None:
    tree = ast.parse("import fastapi\n")
    assert "fastapi" in _imported_names(tree)
