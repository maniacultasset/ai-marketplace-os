"""One pure `redact()` for every log line and every Sentry event (RULES.md: logs and errors never
hold secret values or personal data).

Two layers:
- **Keys.** A value under a denied key (case-insensitive, `-` read as `_`) becomes `REDACTED`:
  exact names (`DENY_KEYS`) and name suffixes (`DENY_SUFFIXES`). They cover every secret Setting
  and every DATABASE.md column holding a secret or a contact detail. Exact names only for the
  short ones, so `tokens_in`, `tokens_out`, `idempotency_key` and `request_id` survive.
- **Values.** Inside any string: `Bearer <token>`, JWT-shaped values, URL userinfo, denied query
  parameters (in URLs and in bare `a=1&b=2` strings) and the values of a Postgres
  `Key (...)=(...)` DETAIL, which a unique violation puts into the traceback.

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
        "state_hash",
        "code_verifier",
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
    {"code", "spapi_oauth_code", "oauth_code", "state", "mws_auth_token"}
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


_BEARER: Final = re.compile(r"\b(?i:bearer)(\s{1,8})[A-Za-z0-9\-._~+/]{1,4096}=*")
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


def _userinfo(match: re.Match[str]) -> str:
    whole = match.group(0)
    if ":" in whole[3:]:
        return f"://{match.group(1)}:{REDACTED}@"
    return f"://{REDACTED}@"


def _query(match: re.Match[str]) -> str:
    lead, name, value = match.group(1), match.group(2), match.group(3)
    if value and value != REDACTED and _is_denied_query_key(name):
        return f"{lead}{name}={REDACTED}"
    return match.group(0)


def _truncate(text: str) -> str:
    if len(text) <= MAX_STR_CHARS:
        return text
    return text[:HEAD_CHARS] + TRUNCATED_MARKER + text[-TAIL_CHARS:]


def redact_text(text: str) -> str:
    """The value patterns, on a string cut to `MAX_STR_CHARS` first."""
    text = _truncate(text)
    if "Key (" in text:
        text = _DETAIL.sub(lambda m: f"Key ({m.group(1)})=({REDACTED})", text)
    if "eyJ" in text:
        text = _JWT.sub(REDACTED, text)
    text = _BEARER.sub(lambda m: f"Bearer{m.group(1)}{REDACTED}", text)
    if "://" in text:
        text = _USERINFO.sub(_userinfo, text)
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
