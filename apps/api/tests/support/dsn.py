"""Test DSNs (CODE-PLAN S10 Q5): the app under test logs in as `app_rw`, never a superuser, which
ignores RLS even under FORCE (DATABASE.md:129). Fixtures and migrations keep the admin login.

`as_app_rw(url)` swaps the credentials of a test-cluster URL for `app_rw` and the CI-only
password the root Makefile sets for it (`CI_APP_RW_PASSWORD`, S9 D7), read from the Makefile so
the value has one home. It is usable only against the local and CI test clusters."""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path
from urllib.parse import quote

from tests.support.paths import repo_root as find_repo_root

REPO_ROOT = find_repo_root(Path(__file__))
_MAKEFILE_LINE = re.compile(r"^CI_APP_RW_PASSWORD := (?P<value>\S+)$", re.M)
_CREDENTIALS = re.compile(r"^(?P<scheme>[a-z+]+://)[^@/]*@")


@cache
def ci_app_rw_password() -> str:
    found = _MAKEFILE_LINE.search((REPO_ROOT / "Makefile").read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("the root Makefile sets no CI_APP_RW_PASSWORD")
    return found.group("value")


def as_app_rw(url: str) -> str:
    """`url` with its user and password replaced by `app_rw` and the CI-only password."""
    if not _CREDENTIALS.match(url):
        raise ValueError("expected a URL of the form scheme://user[:password]@host/db")
    return _CREDENTIALS.sub(
        lambda m: f"{m.group('scheme')}app_rw:{quote(ci_app_rw_password(), safe='')}@", url, count=1
    )
