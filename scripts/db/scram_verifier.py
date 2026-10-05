"""Print a PostgreSQL SCRAM-SHA-256 verifier for a role password (EXECUTION-PLAN.md:545).

`scripts/db/0000_role_passwords.sql` sets role passwords only from verifiers, so no plaintext
password reaches the server or its logs. Usage:

    uv run --frozen python scripts/db/scram_verifier.py < password-file
    uv run --frozen python scripts/db/scram_verifier.py        # prompts, no echo

The password comes on stdin (or the terminal prompt), never as an argument, so it never sits in a
process argv or shell history. One trailing newline is stripped. Only printable ASCII is accepted,
because PostgreSQL applies SASLprep to other characters and this helper does not. The format and
the key derivation are RFC 5802 / RFC 7677 with PostgreSQL's default 4096 iterations.
Stdlib only, no environment access.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import secrets
import sys
from typing import Final, TextIO

ITERATIONS: Final = 4096
SALT_BYTES: Final = 16
_USAGE: Final = "usage: scram_verifier.py < password\n"


def _checked(password: str) -> str:
    """The password itself, or ValueError. The message never contains the value."""
    if not password:
        raise ValueError("the password is empty")
    if any(not 0x20 <= ord(char) <= 0x7E for char in password):
        raise ValueError("the password must be printable ASCII (0x20-0x7E)")
    return password


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def scram_sha256_verifier(
    password: str, *, salt: bytes | None = None, iterations: int = ITERATIONS
) -> str:
    """`SCRAM-SHA-256$<iter>:<salt>$<StoredKey>:<ServerKey>`, the form `pg_authid` stores."""
    secret = _checked(password).encode("ascii")
    salt = secrets.token_bytes(SALT_BYTES) if salt is None else salt
    salted = hashlib.pbkdf2_hmac("sha256", secret, salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    return f"SCRAM-SHA-256${iterations}:{_b64(salt)}${_b64(stored_key)}:{_b64(server_key)}"


def read_password(stream: TextIO) -> str:
    """All of `stream`, minus exactly one trailing "\\n" or "\\r\\n", checked."""
    text = stream.read()
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    return _checked(text)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        sys.stderr.write(_USAGE)
        return 2
    try:
        if sys.stdin.isatty():
            password = _checked(getpass.getpass("role password: "))
        else:
            password = read_password(sys.stdin)
    except ValueError as exc:
        sys.stderr.write(f"scram_verifier: {exc}\n")
        return 1
    sys.stdout.write(scram_sha256_verifier(password) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
