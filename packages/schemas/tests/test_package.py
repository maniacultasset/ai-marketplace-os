"""Package layout and dependency closure."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import mkos_schemas
from mkos_schemas.base import StrictModel, WireModel
from pydantic import BaseModel

PKG = Path(mkos_schemas.__file__).resolve().parent
EXPECTED_MODULES = {
    "__init__.py",
    "agent.py",
    "auth.py",
    "base.py",
    "codes.py",
    "enums.py",
    "errors.py",
    "export.py",
    "images.py",
    "imports.py",
    "jobs.py",
    "listing.py",
    "paths.py",
    "pipeline.py",
    "publish.py",
    "rules.py",
    "validation.py",
}


def test_module_layout() -> None:
    py_files = {p.name for p in PKG.glob("*.py")}
    assert py_files == EXPECTED_MODULES
    assert (PKG / "py.typed").is_file()


def test_runtime_dependencies() -> None:
    import importlib.metadata

    reqs = importlib.metadata.requires("mkos-schemas") or []
    assert len(reqs) == 1
    assert reqs[0].startswith("pydantic")


def test_import_closure_is_pydantic_only() -> None:
    script = """
import importlib, pkgutil, sys
import mkos_schemas
allowed = {
    'mkos_schemas', 'pydantic', 'pydantic_core', 'typing_extensions',
    'annotated_types', 'typing_inspection',
}
for m in list(sys.modules):
    if m.startswith('mkos_schemas'):
        importlib.import_module(m)
for m in list(sys.modules):
    top = m.split('.')[0]
    if top in ('builtins', '_') or m.startswith('_'):
        continue
    if top in allowed:
        continue
    if top in sys.builtin_module_names:
        continue
    # stdlib heuristic
    import importlib.util
    spec = importlib.util.find_spec(top)
    if spec and spec.origin and 'site-packages' in (spec.origin or ''):
        raise SystemExit(f'unexpected top-level import: {m}')
"""
    subprocess.run([sys.executable, "-c", script], check=True, cwd=PKG.parent.parent.parent)


def test_every_module_imports_cleanly() -> None:
    for name in sorted(EXPECTED_MODULES):
        if name == "__init__.py":
            continue
        mod = name[:-3]
        importlib.import_module(f"mkos_schemas.{mod}")


def test_inbound_models_have_no_tenant_id() -> None:
    for mod_name in EXPECTED_MODULES:
        if mod_name == "__init__.py":
            continue
        mod = importlib.import_module(f"mkos_schemas.{mod_name[:-3]}")
        for _, cls in mod.__dict__.items():
            if not isinstance(cls, type) or not issubclass(cls, StrictModel):
                continue
            if cls in (StrictModel, WireModel):
                continue
            assert "tenant_id" not in cls.model_fields


def test_models_use_the_base_classes() -> None:
    for mod_name in EXPECTED_MODULES:
        if mod_name == "__init__.py":
            continue
        mod = importlib.import_module(f"mkos_schemas.{mod_name[:-3]}")
        for _, cls in mod.__dict__.items():
            if not isinstance(cls, type) or not issubclass(cls, BaseModel):
                continue
            if cls.__module__.startswith("pydantic"):
                continue
            assert issubclass(cls, (StrictModel, WireModel)), cls
