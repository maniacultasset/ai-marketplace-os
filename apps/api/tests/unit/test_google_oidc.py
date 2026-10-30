"""ADR-0020: the Google ID token verifier, PKCE, the browser binding, the authoritative-email rule
and the JWKS cache (`app.core.google_oidc`), against tokens the fake Google signs. No network."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from app.core import google_oidc as g
from tests.support.fake_google import (
    FakeGoogleConfig,
    claims_for,
    jwks,
    mint,
    other_key,
    signing_key,
)

CFG = FakeGoogleConfig()
NONCE = "n-0123456789abcdef0123456789abcdef0123456789"
NOW = 1_900_000_000.0


def _keys() -> dict[str, jwt.PyJWK]:
    return g.parse_jwks(jwks())


def _claims(**over: Any) -> dict[str, Any]:
    base = claims_for(CFG, nonce=NONCE, now=NOW)
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def _verify(token: str, **over: Any) -> g.Identity:
    kwargs: dict[str, Any] = {
        "client_id": CFG.client_id,
        "issuer": CFG.issuer,
        "nonce_hash": g.sha256(NONCE),
        "now": NOW,
        **over,
    }
    return g.verify_id_token(token, _keys(), **kwargs)


def test_valid_token_gives_the_identity() -> None:
    identity = _verify(mint(_claims(email="Asha.Rao@Gmail.com")))
    assert identity.email == "asha.rao@gmail.com"
    assert identity.display_name == "Asha Rao"
    assert (identity.given_name, identity.family_name) == ("Asha", "Rao")
    assert identity.hd is None and identity.sub
    assert "asha" not in repr(identity).lower()


def test_issuer_with_or_without_scheme() -> None:
    google = "https://accounts.google.com"
    for iss in (google, "accounts.google.com"):
        identity = _verify(mint(_claims(iss=iss)), issuer=google)
        assert identity.sub


def _now_claim(delta: float) -> int:
    return int(NOW + delta)


REFUSED: dict[str, tuple[dict[str, Any], str]] = {
    "audience": ({"aud": "someone-else.apps.googleusercontent.com"}, "audience"),
    "issuer": ({"iss": "https://evil.example"}, "issuer"),
    "azp": ({"azp": "someone-else"}, "azp"),
    "expired": ({"exp": _now_claim(-60)}, "expired"),
    "iat future": ({"iat": _now_claim(+120)}, "not_yet_valid"),
    "nonce": ({"nonce": "another-nonce"}, "nonce"),
    "nonce missing": ({"nonce": None}, "nonce"),
    "email unverified": ({"email_verified": False}, "email_unverified"),
    "email_verified string": ({"email_verified": "true"}, "email_unverified"),
    "no email": ({"email": None}, "email"),
    "two @": ({"email": "a@b@gmail.com"}, "email"),
    "non-ascii email": ({"email": "\u0430sha@gmail.com"}, "email"),
    "no sub": ({"sub": None}, "signature_or_claims"),
    "long sub": ({"sub": "1" * 256}, "sub"),
}


@pytest.mark.parametrize("case", list(REFUSED), ids=list(REFUSED))
def test_claim_refusals(case: str) -> None:
    over, reason = REFUSED[case]
    with pytest.raises(g.InvalidIdToken) as info:
        _verify(mint(_claims(**over)))
    assert info.value.reason == reason
    assert "@" not in str(info.value)


def test_exp_within_leeway_passes() -> None:
    assert _verify(mint(_claims(exp=_now_claim(-5))))


def test_alg_none_refused() -> None:
    token = jwt.encode(
        _claims(), None, algorithm="none", headers={"kid": "fake-1"}  # type: ignore[arg-type]
    )
    with pytest.raises(g.InvalidIdToken) as info:
        _verify(token)
    assert info.value.reason == "algorithm"


def test_hs256_confusion_refused() -> None:
    # HS256 signed with the public key's PEM as the secret: the classic key confusion
    from cryptography.hazmat.primitives import serialization

    pem = (
        signing_key()
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    token = jwt.encode(_claims(), pem[:64], algorithm="HS256", headers={"kid": "fake-1"})
    with pytest.raises(g.InvalidIdToken) as info:
        _verify(token)
    assert info.value.reason == "algorithm"


def test_signed_by_another_key_refused() -> None:
    with pytest.raises(g.InvalidIdToken) as info:
        _verify(mint(_claims(), key=other_key()))
    assert info.value.reason == "signature_or_claims"


def test_unknown_kid_is_unknown_key() -> None:
    with pytest.raises(g.UnknownKey):
        _verify(mint(_claims(), kid="rotated-2"))


def test_malformed_token_refused() -> None:
    with pytest.raises(g.InvalidIdToken) as info:
        _verify("not.a.jwt")
    assert info.value.reason == "malformed"


def test_names_trimmed_and_bounded() -> None:
    identity = _verify(mint(_claims(name="  Asha\n  Rao ", given_name="x" * 300)))
    assert identity.display_name == "Asha Rao"
    assert identity.given_name is not None and len(identity.given_name) == g.NAME_MAX


def test_display_name_falls_back_to_given_and_family() -> None:
    identity = _verify(mint(_claims(name=None, given_name="Meera", family_name="Iyer")))
    assert identity.display_name == "Meera Iyer"


# ---- authoritative email ----


@pytest.mark.parametrize(
    ("email", "hd", "expected"),
    [
        ("a@gmail.com", None, True),
        ("a@googlemail.com", None, True),
        ("a@GMAIL.com", None, True),
        ("founder@acmenaturals.in", "acmenaturals.in", True),
        ("founder@acmenaturals.in", "ACMENATURALS.IN", True),
        ("founder@acmenaturals.in", None, False),
        ("founder@acmenaturals.in", "other.in", False),
        ("someone@yahoo.com", None, False),
        ("bad-address", None, False),
    ],
)
def test_is_authoritative(email: str, hd: str | None, expected: bool) -> None:
    assert g.is_authoritative(email, hd) is expected


# ---- PKCE, state and the binding ----


def test_authorize_url_shape() -> None:
    verifier = g.new_verifier()
    assert 43 <= len(verifier) <= 128
    url = g.authorize_url(
        "https://accounts.google.com/o/oauth2/v2/auth",
        client_id="cid",
        redirect_uri="https://kuinn.in/api/v1/auth/google/callback",
        state="st",
        nonce="nn",
        verifier=verifier,
    )
    parts = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://accounts.google.com/o/oauth2/v2/auth"
    )
    assert query == {
        "client_id": "cid",
        "redirect_uri": "https://kuinn.in/api/v1/auth/google/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "state": "st",
        "nonce": "nn",
        "code_challenge": g.s256(verifier),
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    assert verifier not in url


def test_binding_round_trip() -> None:
    secret = g.new_token()
    b = g.b64url(g.sha256(secret))
    stored = g.binding_hash(b)
    assert stored is not None and len(stored) == 32
    assert g.binding_matches(secret, stored)
    assert not g.binding_matches(secret + "x", stored)
    assert not g.binding_matches("é", stored)


@pytest.mark.parametrize("b", ["", "a" * 42, "a" * 44, "+" * 43, "a" * 42 + "="])
def test_binding_hash_refuses_malformed(b: str) -> None:
    assert g.binding_hash(b) is None


# ---- JWKS ----


def test_parse_jwks_skips_non_rsa_and_non_sig() -> None:
    document = jwks()
    document["keys"] += [
        {"kty": "EC", "kid": "ec"},
        {**document["keys"][0], "kid": "enc", "use": "enc"},
    ]
    assert set(g.parse_jwks(document)) == {"fake-1"}
    with pytest.raises(ValueError, match="not a JWKS"):
        g.parse_jwks({"nope": []})


def test_jwks_cache_ttl_and_miss_throttle() -> None:
    clock = [1000.0]
    cache = g.JwksCache(clock=lambda: clock[0])
    assert not cache.fresh()
    cache.store(_keys())
    assert cache.fresh()
    clock[0] += g.JWKS_TTL_SECONDS
    assert not cache.fresh()
    assert cache.may_refetch_for_miss() is True
    assert cache.may_refetch_for_miss() is False
    clock[0] += g.JWKS_MISS_REFETCH_SECONDS
    assert cache.may_refetch_for_miss() is True


def test_real_clock_default_verifies_fresh_token() -> None:
    now = time.time()
    claims = claims_for(CFG, nonce=NONCE, now=now)
    identity = g.verify_id_token(
        mint(claims),
        _keys(),
        client_id=CFG.client_id,
        issuer=CFG.issuer,
        nonce_hash=g.sha256(NONCE),
    )
    assert identity.email == "asha.rao@gmail.com"


@pytest.mark.parametrize("value", ["é", "\ud83d", "a\x00b"])
def test_sha256_takes_any_text(value: str) -> None:
    assert len(g.sha256(value)) == 32
    assert not g.binding_matches(value, g.sha256("other"))


def test_binding_hashes_the_secrets_text_not_its_bytes() -> None:
    # The contract vector the app's tests use (docs/HANDOFF-FE.md): `b` is the sha256 of the
    # 43-character base64url text, never of the 32 raw bytes it encodes.
    secret = "A" * 43  # base64url of 32 zero bytes
    b = "DwBzhbb51LfusnSGBa_hqYSgo7-j8BTQnip4TOnlzRo"
    stored = g.binding_hash(b)
    assert stored is not None
    assert g.binding_matches(secret, stored)
    assert g.b64url(g.sha256(secret)) == b
    raw_bytes_hash = "Zmh6rfhivXdsj8GLjp-OIAiXFIVu4jOzkCpZHQ1fKSU"
    other = g.binding_hash(raw_bytes_hash)
    assert other is not None and not g.binding_matches(secret, other)
