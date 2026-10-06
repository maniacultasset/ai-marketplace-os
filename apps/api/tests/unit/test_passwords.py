"""argon2id password hashing through pwdlib (CANON-ADDENDUM.md:113; ENV.md PASSWORD_HASH_SCHEME)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from app.core.passwords import hash_password, verify_password

PASSWORDS_PY = Path(__file__).resolve().parents[2] / "app/core/passwords.py"


def test_hash_is_argon2id() -> None:
    assert hash_password("correct horse").startswith("$argon2id$")


def test_verify_roundtrip() -> None:
    assert verify_password("correct horse", hash_password("correct horse")) is True


def test_verify_wrong_password_false() -> None:
    assert verify_password("wrong horse", hash_password("correct horse")) is False


def test_hash_salted_differs() -> None:
    assert hash_password("same") != hash_password("same")


def test_unicode_password() -> None:
    hashed = hash_password("pässwörd-नमस्ते")
    assert verify_password("pässwörd-नमस्ते", hashed) is True
    assert verify_password("passwort-नमस्ते", hashed) is False


def test_empty_password_raises_valueerror() -> None:
    with pytest.raises(ValueError, match="empty"):
        hash_password("")


@pytest.mark.parametrize("garbage", ["", "not-a-hash", "$argon2id$v=19$broken", "$2b$12$abc"])
def test_verify_garbage_hash_false(garbage: str) -> None:
    assert verify_password("anything", garbage) is False


def test_no_passlib_import() -> None:
    tree = ast.parse(PASSWORDS_PY.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    assert not [n for n in names if n.split(".")[0] == "passlib"]
