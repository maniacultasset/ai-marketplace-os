"""One pure `redact()` for every log line and every Sentry event (RULES.md: logs and errors never
hold secret values or personal data).

Two layers:
- **Keys.** A value under a denied key (case-insensitive, `-` read as `_`) becomes `REDACTED`:
  exact names (`DENY_KEYS`) and name suffixes (`DENY_SUFFIXES`). They cover every secret Setting
  and every DATABASE.md column holding a secret or a contact detail. Exact names only for the
  short ones, so `tokens_in`, `tokens_out`, `idempotency_key` and `request_id` survive.
- **Values.** Inside any string: `Bearer <token>` (and `Basic` / `Token` after an
  `Authorization` key), JWT-shaped values, URL userinfo, denied query
  parameters (in URLs and in bare `a=1&b=2` strings) and the values of a Postgres
  `Key (...)=(...)` DETAIL, which a unique violation puts into the traceback, the
  `Failing row contains (...)` DETAIL of a NOT NULL or CHECK violation, and the
  `[parameters: ...]` that SQLAlchemy appends to a database error, and the quoted value of a
  denied key in JSON, dict-repr or kwarg-repr text, and the `input_value=` of a pydantic
  ValidationError.

Bounded time on attacker-chosen text: a string longer than `MAX_STR_CHARS` keeps its head and its
tail (a traceback's raise site is at the end) around a marker before any pattern runs, and every
pattern is led by a literal and uses bounded, non-overlapping quantifiers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final, cast

REDACTED: Final = "[REDACTED]"
MAX_STR_CHARS: Final = 16_384
HEAD_CHARS: Final = 4_096
TAIL_CHARS: Final = MAX_STR_CHARS - HEAD_CHARS
TRUNCATED_MARKER: Final = "…[TRUNCATED]…"
MAX_DEPTH: Final = 32

DENY_KEYS: Final[frozenset[str]] = frozenset(
    {
        # credentials and session material
        "authorization",
        "proxy_authorization",
        "cookie",
        "cookies",
        "set_cookie",
        "password",
        "hashed_password",
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "token_enc",
        "token_hash",
        "x_amz_access_token",  # the SP-API access-token header (S20 T10)
        "state_hash",
        "code_verifier",
        "code_challenge",
        "pending_handle",
        "secret",
        "api_key",
        "secret_key",
        # secret Settings that carry no telling suffix
        "jwt_secret",
        "sentry_dsn",
        "database_url",
        "alembic_database_url",
        "kms_local_dev_key",
        # personal data and client addresses
        "email",
        "phone",
        "gstin",
        "ip",
        "ip_address",
        "remote_addr",
        "user_agent",
        "actor_email",
        "login_email",
        "x_forwarded_for",
        "x_real_ip",
        "forwarded",
    }
)
DENY_SUFFIXES: Final[tuple[str, ...]] = (
    "_secret",
    "_password",
    "_api_key",
    "_scram",
    "_email",
    "_emails",
    "_phone",
    "_ip",
    "_token",
    "_token_hash",
    "_secret_key",
    "_webhook",
)
# Query parameter names refused on top of the denied keys: the OAuth callback (CANON.md:36)
# carries the authorization code and state. `code` and `state` are query-only: as dict keys
# they stay (the error envelope `code`, a listing `state`).
QUERY_ONLY_DENY_KEYS: Final[frozenset[str]] = frozenset(
    # `pending`: the OAuth callback's opaque confirm handle (S15), in its 303 Location
    {"code", "spapi_oauth_code", "oauth_code", "state", "mws_auth_token", "pending"}
)
QUERY_DENY_KEYS: Final[frozenset[str]] = DENY_KEYS | QUERY_ONLY_DENY_KEYS


def _normalise(key: str) -> str:
    return key.casefold().replace("-", "_")


def is_denied_key(key: object) -> bool:
    """True when a value under `key` must never be logged or sent. Non-str keys never are."""
    if isinstance(key, bytes):
        key = key.decode("latin-1")
    if not isinstance(key, str):
        return False
    name = _normalise(key)
    return name in DENY_KEYS or name.endswith(DENY_SUFFIXES)


def _is_denied_query_key(name: str) -> bool:
    return is_denied_key(name) or _normalise(name) in QUERY_ONLY_DENY_KEYS


# A credential runs to the next space, quote, comma or semicolon: an LWA token is `Atza|...`, so a
# base64url-only class would keep everything after the bar.
_CREDENTIAL: Final = r"[^\s\"',;]{1,4096}"
_BEARER: Final = re.compile(r"\b(?i:bearer)(\s{1,8})" + _CREDENTIAL)
# `Basic` and `Token` are ordinary words, so only after an `Authorization` key (header or quoted).
_AUTH_SCHEME: Final = re.compile(
    r"\b((?i:authorization)[\"']?\s{0,4}[:=]\s{0,4}[\"']?(?i:basic|token)\s{1,8})" + _CREDENTIAL
)
_JWT: Final = re.compile(
    r"eyJ[A-Za-z0-9_-]{1,4096}\.[A-Za-z0-9_-]{1,4096}(?:\.[A-Za-z0-9_-]{0,4096})?"
)
# `scheme://user:password@` keeps the user; `scheme://token@` (no colon) loses the whole userinfo.
_USERINFO: Final = re.compile(r"://([^\s/?#@:]{1,256})(?::[^\s/?#@]{1,256})?@")
# A `name=value` parameter, after `?`, `&`, `;`, whitespace or at the start of the string.
_QUERY: Final = re.compile(r"(^|[?&;\s])([A-Za-z0-9_.\-]{1,64})=([^&#\s\"'<>]{0,1024})")
_DETAIL: Final = re.compile(
    r"Key \(([^=\n]{1,512})\)=\("
    r"(?:[^\n]{0,1024}?\)(?= already exists| is not present| is still referenced| conflicts"
    r"|[.,;\n]|$)|[^\n]{0,1024})"
)


# A NOT NULL or CHECK violation's DETAIL lists the whole row (emails, password hashes, token
# bytes); psycopg puts it into str(exc) and SQLAlchemy's error text repeats it.
_FAILING_ROW: Final = re.compile(r"Failing row contains \([^\n]{0,8192}")
# SQLAlchemy appends the bound parameters to a DBAPI error's text (`[parameters: {...}]`): an
# INSERT's password hash or email would ride the traceback into a log line or a Sentry event.
_SQL_PARAMETERS: Final = re.compile(r"\[parameters: [^\n]{0,8192}")
# A key, then `:` or `=`, then an opening quote: JSON (`"access_token": "..."`), a dict repr
# (`'refresh_token': '...'`), a kwarg repr (`jwt_secret='...'`) and pydantic's `input_value=`,
# with JSON-in-a-string escapes (`\"key\": \"...\"`). Only the key and the opening quote are
# matched, so a harmless key's value is still scanned for a denied key inside it. A string
# prefix (`b'...'` for a bytes repr: `token_hash`, `token_enc`, `state_hash`) sits outside group 2.
# A container value (`[...]`, `(...)`, `{...}`, `bytearray(...)`: a list Setting such as
# `admin_staff_emails`, bytes in a list) opens with group 3 and goes whole, brackets included.
_QUOTED_KEY: Final = re.compile(
    r"(?<![A-Za-z0-9_.\-])(?:\\{0,2}[\"'])?([A-Za-z0-9_.\-]{1,64})(?:\\{0,2}[\"'])?"
    r"\s{0,4}[:=]\s{0,4}(?:(?:[bBrRuUfF]{1,2})?(\\{0,2}[\"'])|(bytearray\(|[\[({]))"
)
MAX_QUOTED_VALUE: Final = 4_096
# pydantic's ValidationError quotes the rejected input, cut in the middle (`{'email': 'a@b...: 'x'}`),
# so a value can lose its key: the whole input goes, up to `, input_type=` or the end of the line.
_INPUT_VALUE: Final = re.compile(r"input_value=(?:[^\n,]|,(?! input_type=)){0,8192}")


def _userinfo(match: re.Match[str]) -> str:
    whole = match.group(0)
    if ":" in whole[3:]:
        return f"://{match.group(1)}:{REDACTED}@"
    return f"://{REDACTED}@"


def _query(match: re.Match[str]) -> str:
    lead, name, value = match.group(1), match.group(2), match.group(3)
    if value and not value.startswith(REDACTED) and _is_denied_query_key(name):
        return f"{lead}{name}={REDACTED}"
    return match.group(0)


def _closing_quote(text: str, opening: str, start: int, limit: int) -> int:
    """The index of the quote that closes a value opened by `opening` (a quote after 0-2
    backslashes), or `limit`. A bare quote closes only after an even backslash run, so `\\"` inside
    a JSON value does not end it; an escaped quote (`\\"` in JSON-in-a-string) closes only after
    exactly its own backslashes. Anything else runs to `limit`: over-redaction, never a leak."""
    quote, escapes = opening[-1], len(opening) - 1
    end = text.find(quote, start, limit)
    while end != -1:
        run = 0
        while end - run - 1 >= start and text[end - run - 1] == "\\":
            run += 1
        if (run % 2 == 0) if escapes == 0 else (run == escapes):
            return end - escapes
        end = text.find(quote, end + 1, limit)
    return limit


def _closing_bracket(text: str, start: int, limit: int) -> int:
    """The index just past the bracket that closes a container opened right before `start`, or
    `limit`. Brackets nest; quoted spans are skipped, so `']'` inside a string does not close it.
    An unbalanced or unclosed value runs to `limit`: over-redaction, never a leak."""
    depth, i = 1, start
    while i < limit:
        char = text[i]
        if char in "\"'":
            run = 0
            while i - run - 1 >= start and text[i - run - 1] == "\\":
                run += 1
            escapes = min(run, 2)
            close = _closing_quote(text, "\\" * escapes + char, i + 1, limit)
            if close >= limit:
                return limit
            i = close + escapes + 1
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return limit


def _quoted_values(text: str) -> str:
    """A denied key's quoted value, up to its closing quote, or its container value, brackets
    included, up to the matching bracket (or the end of the line, or `MAX_QUOTED_VALUE`
    characters when it never closes), becomes `REDACTED`. Dict-key
    rules apply, so `'code': 'NOT_FOUND'` (the error envelope) stays."""
    out: list[str] = []
    pos = 0
    for match in _QUOTED_KEY.finditer(text):
        if match.start() < pos or not is_denied_key(match.group(1)):
            continue
        start = match.end()
        limit = min(len(text), start + MAX_QUOTED_VALUE)
        newline = text.find("\n", start, limit)
        if newline != -1:
            limit = newline
        if match.group(3) is not None:
            start = match.start(3)
            end = _closing_bracket(text, match.end(3), limit)
        else:
            end = _closing_quote(text, match.group(2), start, limit)
        out.append(text[pos:start])
        out.append(REDACTED)
        pos = end
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)


def _truncate(text: str) -> str:
    if len(text) <= MAX_STR_CHARS:
        return text
    return text[:HEAD_CHARS] + TRUNCATED_MARKER + text[-TAIL_CHARS:]


def redact_text(text: str) -> str:
    """The value patterns, on a string cut to `MAX_STR_CHARS` first."""
    text = _truncate(text)
    if "[parameters: " in text:
        text = _SQL_PARAMETERS.sub(f"[parameters: {REDACTED}]", text)
    if "Failing row contains (" in text:
        text = _FAILING_ROW.sub(f"Failing row contains ({REDACTED})", text)
    if "input_value=" in text:
        text = _INPUT_VALUE.sub(f"input_value={REDACTED}", text)
    if "Key (" in text:
        text = _DETAIL.sub(lambda m: f"Key ({m.group(1)})=({REDACTED})", text)
    if "eyJ" in text:
        text = _JWT.sub(REDACTED, text)
    text = _BEARER.sub(lambda m: f"Bearer{m.group(1)}{REDACTED}", text)
    if "authorization" in text.casefold():
        text = _AUTH_SCHEME.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    if "://" in text:
        text = _USERINFO.sub(_userinfo, text)
    if ":" in text or "=" in text:
        text = _quoted_values(text)
    if "=" in text:
        text = _QUERY.sub(_query, text)
    return text


def _redact_bytes(value: bytes) -> bytes:
    decoded = value.decode("latin-1")
    cleaned = redact_text(decoded)
    return value if cleaned == decoded else cleaned.encode("latin-1", "replace")


def _is_header_pair(value: list[Any] | tuple[Any, ...]) -> bool:
    return len(value) == 2 and isinstance(value[0], str | bytes) and is_denied_key(value[0])


def _walk(value: Any, depth: int) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        if depth >= MAX_DEPTH:
            return REDACTED
        return {
            k: (REDACTED if v is not None else None) if is_denied_key(k) else _walk(v, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        if depth >= MAX_DEPTH:
            return REDACTED
        if _is_header_pair(value):
            items: list[Any] = [value[0], REDACTED]
        else:
            items = [_walk(item, depth + 1) for item in value]
        return items if isinstance(value, list) else tuple(items)
    if isinstance(value, bytes):
        return _redact_bytes(value)
    return value


def redact[T](value: T) -> T:
    """A redacted copy of `value` (dicts, lists, tuples and strings are rebuilt; the input is
    never mutated). Other types come back as they are."""
    return cast(T, _walk(value, 0))
