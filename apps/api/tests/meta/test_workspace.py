"""AC2: the uv workspace, its pins and the build-time constraints."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

BANNED_PACKAGES = (
    "asyncpg",
    "redis",
    "arq",
    "python-jose",
    "passlib",
    "numpy",
    "cloud-sql-python-connector",
    "gunicorn",
    "slowapi",
)
BUILD_CONSTRAINT_NAMES = {
    "hatchling",
    "packaging",
    "pathspec",
    "pluggy",
    "trove-classifiers",
    "pip",
    "editables",
}
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)")


def _toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _lock_package_names(repo_root: Path) -> set[str]:
    lock = _toml(repo_root / "uv.lock")
    return {pkg["name"] for pkg in lock["package"]}


def _build_constraints(repo_root: Path) -> dict[str, tuple[str, list[str]]]:
    """name -> (version, hashes). Fails on any unpinned or unhashed requirement."""
    text = (repo_root / "build-constraints.txt").read_text(encoding="utf-8")
    joined = re.sub(r"\\\n", " ", text)
    pins: dict[str, tuple[str, list[str]]] = {}
    for raw in joined.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = _PIN.match(line)
        assert match, f"unpinned requirement: {line!r}"
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", line)
        assert hashes, f"unhashed requirement: {line!r}"
        name = match.group(1).lower()
        assert name not in pins, f"duplicate requirement: {name}"
        pins[name] = (match.group(2), hashes)
    return pins


def test_members_exactly_api_and_schemas(repo_root: Path) -> None:
    root = _toml(repo_root / "pyproject.toml")
    assert "project" not in root, "the workspace root is virtual"
    assert root["tool"]["uv"]["workspace"]["members"] == ["apps/api", "packages/schemas"]


def test_requires_python_313_in_every_member(repo_root: Path) -> None:
    for member in ("apps/api", "packages/schemas"):
        project = _toml(repo_root / member / "pyproject.toml")["project"]
        assert project["requires-python"] == "==3.13.*", member


def test_python_version_file_is_313(repo_root: Path) -> None:
    assert (repo_root / ".python-version").read_text(encoding="utf-8") == "3.13\n"


def test_runtime_pins_match_adr_0005(repo_root: Path) -> None:
    deps = _toml(repo_root / "apps/api/pyproject.toml")["project"]["dependencies"]
    by_name = {re.split(r"[\[=<>!~ ]", d, maxsplit=1)[0].lower(): d for d in deps}
    assert re.fullmatch(r"fastapi==\d+\.\d+\.\d+", by_name["fastapi"])
    assert re.fullmatch(r"uvicorn==\d+\.\d+\.\d+", by_name["uvicorn"])
    assert by_name["pydantic"] == "pydantic>=2.13.5,<2.14"
    assert by_name["pydantic-settings"] == "pydantic-settings>=2.14.2"
    assert by_name["sqlalchemy"] == "sqlalchemy[asyncio]>=2.0.54,<2.1"
    assert by_name["starlette"] == "starlette>=1.3.1"
    assert by_name["psycopg"] == "psycopg[binary]>=3.2"
    assert by_name["mkos-schemas"] == "mkos-schemas"
    # CP2 (S3): FastAPI's File/Form params need it; exact pin, pip-audit clean.
    assert re.fullmatch(r"python-multipart==\d+\.\d+\.\d+", by_name["python-multipart"])


def test_multipart_importable() -> None:
    # FastAPI refuses a File/Form route at definition time without python-multipart (CANON.md:83).
    import python_multipart
    from fastapi import FastAPI, File, UploadFile

    assert python_multipart.__version__
    app = FastAPI()

    @app.post("/upload")
    async def upload(file: UploadFile = File(...)) -> None:  # noqa: B008 - FastAPI idiom
        return None


def test_api_runtime_pins_alembic_and_pgvector(repo_root: Path) -> None:
    # The image runs migrations (APP_ROLE=migrate) and S7 maps Vector(1536), so both are runtime.
    deps = _toml(repo_root / "apps/api/pyproject.toml")["project"]["dependencies"]
    by_name = {re.split(r"[\[=<>!~ ]", d, maxsplit=1)[0].lower(): d for d in deps}
    assert by_name["alembic"] == "alembic>=1.20,<2"
    assert by_name["pgvector"] == "pgvector>=0.5,<0.6"


# squawk-cli 2.65.0 wheels (PyPI sha256), the newest release at least 14 days old at S5.
SQUAWK_WHEEL_SHA256 = frozenset(
    {
        "9afb0db939ff7de10e02c4d7a0752ef6f7e076f6cbb34d479f78e4fc61e1724a",
        "16b1e4ecccbe15958415ab4f2532e12c1694884e9cfe06d005422977a1adae88",
        "d3ce2d832791c8e736454f354b2d1241fb672162d1273933807ba832c2fb30f2",
        "b016159ca6e7be2618e677791fc7476ba08f15b2fe3862986feeaadc8e60b025",
    }
)
SQUAWK_SDIST_SHA256 = "cd429a528ebb55a7eb97e6571d4c029ca672037e356012f72642e2b57b4fd3b6"


def test_lock_resolves_s5_tools(repo_root: Path) -> None:
    packages = {pkg["name"]: pkg for pkg in _toml(repo_root / "uv.lock")["package"]}
    assert re.fullmatch(r"1\.20\.\d+", packages["alembic"]["version"])
    assert re.fullmatch(r"0\.5\.\d+", packages["pgvector"]["version"])
    squawk = packages["squawk-cli"]
    assert squawk["version"] == "2.65.0"
    assert squawk["sdist"]["hash"] == f"sha256:{SQUAWK_SDIST_SHA256}"
    wheel_hashes = {wheel["hash"].removeprefix("sha256:") for wheel in squawk["wheels"]}
    assert wheel_hashes >= SQUAWK_WHEEL_SHA256
    api_deps = {dep["name"] for dep in packages["mkos-api"]["dependencies"]}
    assert {"alembic", "pgvector"} <= api_deps
    dev = _toml(repo_root / "pyproject.toml")["dependency-groups"]["dev"]
    assert "squawk-cli==2.65.0" in dev


def test_lock_pins_procrastinate_3_10_0(repo_root: Path) -> None:
    # ADR-0004 §1: the queue is procrastinate >=3.10,<3.11; revision 0003b vendors 3.10.0's SQL.
    deps = _toml(repo_root / "apps/api/pyproject.toml")["project"]["dependencies"]
    assert "procrastinate>=3.10,<3.11" in deps
    packages = {pkg["name"]: pkg for pkg in _toml(repo_root / "uv.lock")["package"]}
    assert packages["procrastinate"]["version"] == "3.10.0"
    api_deps = {dep["name"] for dep in packages["mkos-api"]["dependencies"]}
    assert "procrastinate" in api_deps


def test_dev_dependencies_exact_pinned(repo_root: Path) -> None:
    dev = _toml(repo_root / "pyproject.toml")["dependency-groups"]["dev"]
    assert dev, "the dev group is empty"
    for requirement in dev:
        assert re.fullmatch(r"[a-z0-9][a-z0-9._-]*==[0-9][0-9a-z.]*", requirement), requirement


@pytest.mark.parametrize("banned", BANNED_PACKAGES)
def test_lockfile_has_no_banned_packages(repo_root: Path, banned: str) -> None:
    assert banned not in _lock_package_names(repo_root)


def test_lockfile_has_no_pipeline_package(repo_root: Path) -> None:
    names = _lock_package_names(repo_root)
    assert not {n for n in names if "pipeline" in n}, "the AI pipeline is pinned in S14 (ADR-0012)"


def test_member_manifests_have_no_readme_key(repo_root: Path) -> None:
    # .dockerignore drops README.md, so a readme key would break the image build.
    for member in ("apps/api", "packages/schemas"):
        assert "readme" not in _toml(repo_root / member / "pyproject.toml")["project"], member


def test_build_constraints_exact_pinned_and_hashed(repo_root: Path) -> None:
    pins = _build_constraints(repo_root)
    assert set(pins) == BUILD_CONSTRAINT_NAMES


def test_build_constraints_agree(repo_root: Path) -> None:
    pins = _build_constraints(repo_root)
    expected = sorted(f"{name}=={version}" for name, (version, _) in pins.items() if name != "pip")
    configured = _toml(repo_root / "pyproject.toml")["tool"]["uv"]["build-constraint-dependencies"]
    assert sorted(configured) == expected
    hatchling = f"hatchling=={pins['hatchling'][0]}"
    for member in ("apps/api", "packages/schemas"):
        build = _toml(repo_root / member / "pyproject.toml")["build-system"]
        assert build["requires"] == [hatchling], member


def test_build_constraints_header_names_editables(repo_root: Path) -> None:
    text = (repo_root / "build-constraints.txt").read_text(encoding="utf-8")
    header, _, _ = text.partition("hatchling==")
    assert "five dependencies" in header
    assert "four" not in header
    assert "editables==" in text
    recipe = next(line for line in text.splitlines() if "printf" in line)
    for pin in (
        "hatchling==",
        "packaging==",
        "pathspec==",
        "pluggy==",
        "trove-classifiers==",
        "pip==",
        "editables==",
    ):
        assert pin in recipe, pin
