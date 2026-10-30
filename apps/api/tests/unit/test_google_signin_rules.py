"""ADR-0020: the pure rules of Google sign-in: the decision table (§6-§7), the brand-name
suggestion (§6) and the redaction of its handles and identity."""

from __future__ import annotations

import pytest
from app.core.redaction import redact, redact_text
from app.services.google_signin import CONSUMER_DOMAINS, Decision, Situation, decide, suggest_brand

VETTED = {"exists": True, "has_membership": True}


@pytest.mark.parametrize(
    ("situation", "expected"),
    [
        # no account with this Google account or email: the prefilled sign-up
        (Situation(by_sub=False, exists=False), Decision("needs_details")),
        # found by the linked Google account
        (Situation(by_sub=True, **VETTED), Decision("sign_in")),
        (Situation(by_sub=True, exists=True, has_request=True), Decision("pending")),
        (Situation(by_sub=True, exists=True), Decision(None, "no_active_membership")),
        (Situation(by_sub=True, is_platform_admin=True, **VETTED), Decision(None, "google_admin")),
        (Situation(by_sub=True, is_active=False, **VETTED), Decision(None, "inactive_user")),
        # found by email (no Google account linked yet): link after the password and code (§7)
        (Situation(by_sub=False, **VETTED), Decision("link_required")),
        (Situation(by_sub=False, is_platform_admin=True, **VETTED), Decision(None, "google_admin")),
        (
            Situation(by_sub=False, linked_to_other=True, **VETTED),
            Decision(None, "google_link_refused"),
        ),
        (
            Situation(by_sub=False, has_password=False, **VETTED),
            Decision(None, "google_link_refused"),
        ),
        (Situation(by_sub=False, is_active=False, **VETTED), Decision(None, "inactive_user")),
        # an applicant (no membership): their request's status, else refused
        (Situation(by_sub=False, exists=True, has_request=True), Decision("pending")),
        (Situation(by_sub=False, exists=True), Decision(None, "google_unvetted_conflict")),
    ],
)
def test_decision_table(situation: Situation, expected: Decision) -> None:
    assert decide(situation) == expected


@pytest.mark.parametrize(
    ("email", "brand"),
    [
        ("asha@acmenaturals.com", "Acmenaturals"),
        ("asha@acme-naturals.co.in", "Acme Naturals"),
        ("asha@acme_naturals.in", "Acme Naturals"),
        ("asha@shop.acme.com", "Acme"),
        ("asha@ACME.IN", "Acme"),
        ("asha@acme.co.uk", "Acme"),
        ("asha@gmail.com", None),
        ("asha@yahoo.co.in", None),
        ("asha@rediffmail.com", None),
        ("asha@proton.me", None),
        ("asha@1234.com", None),
        ("asha@x.com", None),
        ("asha@xn--80ak6aa92e.com", None),
        ("asha@localhost", None),
        ("not-an-email", None),
    ],
)
def test_suggest_brand(email: str, brand: str | None) -> None:
    assert suggest_brand(email) == brand


def test_consumer_list_covers_the_big_providers() -> None:
    assert {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com"} <= (
        CONSUMER_DOMAINS
    )


def test_handles_and_identity_redacted() -> None:
    event = {
        "binding": "secret-binding",
        "details_handle": "dh",
        "link_handle": "lh",
        "nonce": "n",
        "google_sub": "1234567890",
        "link_google_sub": "1234567890",
    }
    assert set(redact(event).values()) == {"[REDACTED]"}
    line = redact_text("GET /auth/google?handle=h-secret-1 and /start?b=bind-secret-2")
    assert "h-secret-1" not in line and "bind-secret-2" not in line
