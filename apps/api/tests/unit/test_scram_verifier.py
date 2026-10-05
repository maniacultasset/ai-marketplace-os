"""scripts/db/scram_verifier.py: SCRAM-SHA-256 verifiers, so no plaintext role password reaches the
server (EXECUTION-PLAN.md:545). Checked against the RFC 7677 §3 example exchange."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import re
from collections.abc import Callable
from types import ModuleType

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

RFC_PASSWORD = "pencil"
RFC_SALT = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
# Split across two literals so no single line looks like a stored credential.
RFC_VERIFIER = (
    "SCRAM-SHA-256$4096:W22ZaJ0SNY7soEsUEjb6gQ==$WG5d8oPm3OtcPnkdi4Uo7BkeZkBFzpcXkuLmtbsT4qY="
    ":wfPLwcE6nTWhTAmQ7tl2KeoiWGPlZqQxSrmfPwDl2dU="
)
RFC_AUTH_MESSAGE = (
    "n=user,r=rOprNGfwEbeRWgbNEkqO,"
    "r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
    "c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0"
)
RFC_CLIENT_PROOF = "dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ="
RFC_SERVER_SIGNATURE = "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="
VERIFIER_FORMAT = re.compile(
    r"^SCRAM-SHA-256\$4096:[A-Za-z0-9+/]{22}==\$[A-Za-z0-9+/]{43}=:[A-Za-z0-9+/]{43}=$"
)
# `_` and `!` are outside the base64 alphabet, so this can never appear inside a verifier.
LEAK = "leakpw_s5!"


@pytest.fixture(scope="module")
def scram(script: Callable[[str], ModuleType]) -> ModuleType:
    return script("scripts/db/scram_verifier.py")


def _parse(verifier: str) -> tuple[int, bytes, bytes, bytes]:
    head, keys = verifier.split("$", 1)[1].split("$")
    iterations, salt = head.split(":")
    stored, server = keys.split(":")
    decode = base64.b64decode
    return int(iterations), decode(salt), decode(stored), decode(server)


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def _run_main(
    scram: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stdin: str,
    argv: list[str] | None = None,
) -> tuple[int, str, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = scram.main([] if argv is None else argv)
    out, err = capsys.readouterr()
    return code, out, err


def test_scram_verifier_rfc7677_vector(scram: ModuleType) -> None:
    assert scram.scram_sha256_verifier(RFC_PASSWORD, salt=RFC_SALT) == RFC_VERIFIER


def test_scram_verifier_client_proof_matches_rfc7677(scram: ModuleType) -> None:
    _, _, stored_key, server_key = _parse(scram.scram_sha256_verifier(RFC_PASSWORD, salt=RFC_SALT))
    message = RFC_AUTH_MESSAGE.encode()
    client_signature = hmac.new(stored_key, message, hashlib.sha256).digest()
    proof = base64.b64decode(RFC_CLIENT_PROOF)
    client_key = bytes(a ^ b for a, b in zip(proof, client_signature, strict=True))
    assert hashlib.sha256(client_key).digest() == stored_key
    server_signature = hmac.new(server_key, message, hashlib.sha256).digest()
    assert base64.b64encode(server_signature).decode() == RFC_SERVER_SIGNATURE


def test_scram_verifier_format_and_random_salt(scram: ModuleType) -> None:
    first = scram.scram_sha256_verifier("pw-s5")
    second = scram.scram_sha256_verifier("pw-s5")
    assert VERIFIER_FORMAT.fullmatch(first)
    assert VERIFIER_FORMAT.fullmatch(second)
    assert first != second
    assert scram.ITERATIONS == 4096
    assert scram.SALT_BYTES == 16


def test_scram_verifier_strips_one_newline(scram: ModuleType) -> None:
    assert scram.read_password(io.StringIO("pw\n")) == "pw"
    assert scram.read_password(io.StringIO("pw\r\n")) == "pw"
    assert scram.read_password(io.StringIO("pw")) == "pw"
    salt = b"\x01" * 16
    assert scram.scram_sha256_verifier(
        scram.read_password(io.StringIO("pw\r\n")), salt=salt
    ) == scram.scram_sha256_verifier("pw", salt=salt)
    with pytest.raises(ValueError):
        scram.read_password(io.StringIO("pw\n\n"))


@pytest.mark.parametrize("raw", ["", "\n", "\r\n"])
def test_scram_verifier_refuses_empty_and_blank(scram: ModuleType, raw: str) -> None:
    with pytest.raises(ValueError, match="empty"):
        scram.read_password(io.StringIO(raw))


@pytest.mark.parametrize("raw", ["é", "　", "a\tb", "a\x00b", "a\rb", "\U0001f600", "x\x7f"])
def test_scram_verifier_refuses_non_ascii_and_control(scram: ModuleType, raw: str) -> None:
    with pytest.raises(ValueError, match="printable ASCII") as caught:
        scram.read_password(io.StringIO(raw))
    assert raw not in str(caught.value)
    with pytest.raises(ValueError, match="printable ASCII"):
        scram.scram_sha256_verifier(raw)


def test_scram_verifier_keeps_spaces(scram: ModuleType) -> None:
    assert scram.read_password(io.StringIO(" a b ")) == " a b "
    salt = b"\x02" * 16
    spaced = scram.scram_sha256_verifier(" a b ", salt=salt)
    assert spaced != scram.scram_sha256_verifier("a b", salt=salt)


def test_scram_verifier_takes_no_arguments(
    scram: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run_main(scram, monkeypatch, capsys, LEAK + "\n", argv=["x"])
    assert code == 2
    assert out == ""
    assert err == "usage: scram_verifier.py < password\n"


def test_scram_verifier_tty_uses_getpass(
    scram: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prompts: list[str] = []

    def fake_getpass(prompt: str = "Password: ") -> str:
        prompts.append(prompt)
        return LEAK

    monkeypatch.setattr("sys.stdin", _TTY("never read"))
    monkeypatch.setattr(scram.getpass, "getpass", fake_getpass)
    assert scram.main([]) == 0
    out, err = capsys.readouterr()
    assert len(prompts) == 1
    assert VERIFIER_FORMAT.fullmatch(out.rstrip("\n"))
    assert LEAK not in out + err + prompts[0]


@pytest.mark.parametrize(
    ("stdin", "code"),
    [
        (LEAK + "\n", 0),
        (LEAK + "\r\n", 0),
        (LEAK + "\n\n", 1),
        (LEAK + "é", 1),
        (LEAK + "\t", 1),
        ("", 1),
    ],
)
def test_scram_verifier_never_echoes_plaintext(
    scram: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stdin: str,
    code: int,
) -> None:
    rc, out, err = _run_main(scram, monkeypatch, capsys, stdin)
    assert rc == code
    assert LEAK not in out
    assert LEAK not in err
    if code == 0:
        assert out.endswith("\n")
        assert VERIFIER_FORMAT.fullmatch(out.rstrip("\n"))
        assert err == ""
    else:
        assert out == ""
        assert len(err.splitlines()) == 1


@settings(max_examples=60, deadline=None)
@given(
    password=st.text(
        alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E), min_size=1, max_size=64
    )
)
def test_scram_verifier_verifies_any_ascii_password(
    script: Callable[[str], ModuleType], password: str
) -> None:
    scram = script("scripts/db/scram_verifier.py")
    iterations, salt, stored_key, _ = _parse(scram.scram_sha256_verifier(password))
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("ascii"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    assert hashlib.sha256(client_key).digest() == stored_key
