"""The password rules of Amazon DPP 1.4.1, read literally (DPP-1, ADR-0021).

- 12 to 1024 characters (code points), with an upper-case letter, a lower-case letter, a digit and
  a symbol (any character that is neither a letter nor a digit; a space counts).
- No part of the user's name: the email local part (3+ characters) and every letter run of 3+
  characters in the local part or the display name, compared after NFKC and case folding.
- Minimum age 1 day (self-service changes), maximum age 365 days, and none of the last 10
  passwords (the current one plus the 9 kept in `user_password_history`; checked by the callers,
  which hold the hashes).

`check` returns every broken rule. Each message is a fixed sentence naming the rule, never the
password or the name, so it can go into a 422 body or a CLI error as it is.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

PASSWORD_MIN: Final = 12
PASSWORD_MAX: Final = 1024
MIN_AGE: Final = timedelta(days=1)
MAX_AGE: Final = timedelta(days=365)
HISTORY: Final = 10
PERSONAL_MIN: Final = 3

Rule = Literal["length", "upper", "lower", "digit", "symbol", "personal", "reused"]

MESSAGES: Final[dict[Rule, str]] = {
    "length": f"The password must be {PASSWORD_MIN} to {PASSWORD_MAX} characters long.",
    "upper": "The password must contain an upper-case letter.",
    "lower": "The password must contain a lower-case letter.",
    "digit": "The password must contain a digit.",
    "symbol": "The password must contain a symbol or a space.",
    "personal": "The password must not contain your name or the first part of your email.",
    # checked by the callers, which hold the hashes (`check` never returns it)
    "reused": "The password must not be one of the last 10.",
}


@dataclass(frozen=True)
class Breach:
    rule: Rule
    message: str


class PolicyError(ValueError):
    """A password that breaks one or more rules. Its text is the fixed rule messages only."""

    def __init__(self, breaches: list[Breach]) -> None:
        self.breaches = breaches
        super().__init__("; ".join(b.message for b in breaches))


def reused() -> PolicyError:
    """The history rule's refusal: the new password is one of the last 10."""
    return PolicyError([Breach("reused", MESSAGES["reused"])])


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _is_letter(ch: str) -> bool:
    # Letters plus combining marks, so a Devanagari vowel sign stays inside its word.
    return unicodedata.category(ch)[0] in ("L", "M")


def _is_digit(ch: str) -> bool:
    return unicodedata.category(ch) == "Nd"


def _letter_runs(text: str) -> list[str]:
    runs: list[str] = []
    current: list[str] = []
    for ch in text:
        if _is_letter(ch):
            current.append(ch)
            continue
        if current:
            runs.append("".join(current))
            current = []
    if current:
        runs.append("".join(current))
    return runs


def _personal_tokens(email: str, display_name: str | None) -> set[str]:
    local = _fold(email.rpartition("@")[0] if "@" in email else email)
    tokens: set[str] = set()
    if len(local) >= PERSONAL_MIN:
        tokens.add(local)
    for source in (local, _fold(display_name or "")):
        tokens.update(run for run in _letter_runs(source) if len(run) >= PERSONAL_MIN)
    return tokens


def check(plain: str, *, email: str, display_name: str | None) -> list[Breach]:
    """Every rule `plain` breaks, in a fixed order; empty when it complies."""
    text = unicodedata.normalize("NFKC", plain)
    broken: list[Rule] = []
    if not PASSWORD_MIN <= len(plain) <= PASSWORD_MAX:
        broken.append("length")
    if not any(ch.isupper() for ch in text):
        broken.append("upper")
    if not any(ch.islower() for ch in text):
        broken.append("lower")
    if not any(_is_digit(ch) for ch in text):
        broken.append("digit")
    if not any(not _is_letter(ch) and not _is_digit(ch) for ch in text):
        broken.append("symbol")
    folded = _fold(plain)
    if any(token in folded for token in _personal_tokens(email, display_name)):
        broken.append("personal")
    return [Breach(rule, MESSAGES[rule]) for rule in broken]


def too_young(changed_at: datetime, now: datetime) -> bool:
    """True while the current password is under a day old (self-service changes wait)."""
    return now - changed_at < MIN_AGE


def expired(changed_at: datetime, now: datetime) -> bool:
    """True once the current password is 365 days old or more (the next sign-in forces a change)."""
    return now - changed_at >= MAX_AGE
