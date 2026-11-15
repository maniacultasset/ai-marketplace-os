"""Common share links turned into direct downloads (G2 sheet photos): a pure URL rewrite.

A seller often pastes the page link of a shared photo. Two hosts are rewritten, by exact host
match only, never by a suffix or a lookalike:

- Google Drive: `drive.google.com/file/d/<id>/view...`, `drive.google.com/open?id=<id>` and
  `drive.google.com` or `docs.google.com` `/uc?id=<id>` become
  `https://drive.google.com/uc?export=download&id=<id>`. The id must match `[A-Za-z0-9_-]{10,200}`.
- Dropbox: `www.dropbox.com` and `dropbox.com` links with `dl=0` or no `dl` get `dl=1`; the path
  and every other query value (`rlkey` among them) are kept.

Anything else comes back unchanged. No auth flow and no network: the rewritten link is what the
fetcher downloads and what the dedupe hashes; the seller's own link is what is stored and shown.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = ["direct_url", "is_drive"]

_DRIVE_ID: Final = re.compile(r"[A-Za-z0-9_-]{10,200}")
_DRIVE_FILE: Final = re.compile(r"^/file/d/([^/]+)(?:/.*)?$")
DRIVE_HOSTS: Final = frozenset({"drive.google.com", "docs.google.com"})
_DROPBOX_HOSTS: Final = frozenset({"www.dropbox.com", "dropbox.com"})


def _host(url: str) -> tuple[str, str, str] | None:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or port not in (None, 443) or parts.username or parts.password:
        return None
    return host, parts.path, parts.query


def _drive_id(path: str, query: str, host: str) -> str | None:
    if host == "drive.google.com":
        match = _DRIVE_FILE.match(path)
        if match is not None:
            return match.group(1)
    if path in ("/open", "/uc") and (host == "drive.google.com" or path == "/uc"):
        values = [v for k, v in parse_qsl(query, keep_blank_values=True) if k == "id"]
        return values[0] if len(values) == 1 else None
    return None


def direct_url(url: str) -> str:
    """The direct-download form of a Drive or Dropbox share link, else `url` itself."""
    found = _host(url)
    if found is None:
        return url
    host, path, query = found
    if host in DRIVE_HOSTS:
        file_id = _drive_id(path, query, host)
        if file_id is None or not _DRIVE_ID.fullmatch(file_id):
            return url
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    if host in _DROPBOX_HOSTS:
        pairs = parse_qsl(query, keep_blank_values=True)
        dl = [v for k, v in pairs if k == "dl"]
        if dl and dl != ["0"]:
            return url
        kept = [(k, v) for k, v in pairs if k != "dl"] + [("dl", "1")]
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))
    return url


def is_drive(url: str) -> bool:
    """The link is on a Google Drive host (for the Drive-specific "not public" message)."""
    found = _host(url)
    return found is not None and found[0] in DRIVE_HOSTS
