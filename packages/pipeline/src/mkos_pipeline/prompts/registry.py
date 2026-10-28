from hashlib import sha256
from pathlib import Path

_ROOT = Path(__file__).parent


def _path(name: str, version: str) -> Path:
    return _ROOT / name / f"{version}.md"


def load_prompt(name: str, version: str) -> str:
    path = _path(name, version)
    if not path.is_file():
        raise FileNotFoundError(f"prompt {name}/{version} not found at {path}")
    return path.read_text(encoding="utf-8")


def prompt_version(name: str, version: str) -> str:
    text = load_prompt(name, version)
    digest = sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{name}/{version}+{digest}"
