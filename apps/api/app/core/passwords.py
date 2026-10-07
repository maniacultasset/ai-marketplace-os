"""Password hashing: argon2id through pwdlib (CANON-ADDENDUM.md:113; ENV.md PASSWORD_HASH_SCHEME).

passlib is forbidden. `verify_password` never raises on a malformed or foreign hash: it is False.
"""

from __future__ import annotations

import getpass
from functools import lru_cache
from typing import Final, TextIO

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from pwdlib.hashers.argon2 import Argon2Hasher
from pydantic import SecretStr


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


PASSWORD_MIN: Final = 12
PASSWORD_MAX: Final = 1024


def read_password(stdin: TextIO) -> SecretStr:
    """A new password for the staff CLI (S16 T19), never from argv or the environment: on a
    terminal it is asked twice (both must match), otherwise it is the first line of stdin. Out of
    12..1024 characters (or a mismatch) is `ValueError`, whose message holds no password."""
    if stdin.isatty():
        first = getpass.getpass("New password: ")
        if getpass.getpass("Repeat it: ") != first:
            raise ValueError("the two passwords differ")
    else:
        first = stdin.readline().rstrip("\r\n")
    if not PASSWORD_MIN <= len(first) <= PASSWORD_MAX:
        raise ValueError(f"the password must be {PASSWORD_MIN}..{PASSWORD_MAX} characters")
    return SecretStr(first)
