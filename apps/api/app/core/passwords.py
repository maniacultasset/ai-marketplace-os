"""Password hashing: argon2id through pwdlib (CANON-ADDENDUM.md:113; ENV.md PASSWORD_HASH_SCHEME).

passlib is forbidden. `verify_password` never raises on a malformed or foreign hash: it is False.
"""

from __future__ import annotations

from functools import lru_cache

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from pwdlib.hashers.argon2 import Argon2Hasher


@lru_cache(maxsize=1)
def _hasher() -> PasswordHash:
    return PasswordHash((Argon2Hasher(),))


def hash_password(plain: str) -> str:
    if not plain:
        raise ValueError("password must not be empty")
    return _hasher().hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return _hasher().verify(plain, hashed)
    except (UnknownHashError, ValueError):
        return False
