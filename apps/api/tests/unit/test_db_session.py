"""DPP-1 T9: `set_gucs(challenge_hash=...)` (app.db.session) with no database: a fake session
records the `set_config` calls and `session.info`."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import pytest
from app.db import session as db_session
from app.db.session import CHALLENGE_HASH_INFO_KEY, set_gucs


class FakeSession:
    def __init__(self) -> None:
        self.info: dict[str, Any] = {}
        self.calls: list[tuple[str, str, bool]] = []

    async def execute(self, statement: Any) -> None:
        params = statement.compile().params
        values = list(params.values())
        self.calls.append((values[0], values[1], values[2]))


DIGEST = hashlib.sha256(b"challenge").digest()


@pytest.mark.parametrize("bad", [b"", b"x" * 31, b"x" * 33, "a" * 32, DIGEST.hex()])
def test_challenge_hash_must_be_32_bytes(bad: object) -> None:
    fake = FakeSession()
    with pytest.raises(ValueError, match="32 bytes"):
        asyncio.run(set_gucs(fake, challenge_hash=bad))  # type: ignore[arg-type]
    assert fake.calls == [] and fake.info == {}


def test_challenge_hash_sent_as_hex() -> None:
    fake = FakeSession()
    asyncio.run(set_gucs(fake, challenge_hash=DIGEST))  # type: ignore[arg-type]
    assert fake.calls == [("app.challenge_hash", DIGEST.hex(), True)]


def test_challenge_hash_kept_in_session_info() -> None:
    fake = FakeSession()
    asyncio.run(set_gucs(fake, challenge_hash=DIGEST))  # type: ignore[arg-type]
    assert fake.info[CHALLENGE_HASH_INFO_KEY] == DIGEST.hex()
    # the after_begin listener re-applies it on every later transaction
    assert (CHALLENGE_HASH_INFO_KEY, "app.challenge_hash") in db_session._GUC_KEYS


def test_challenge_hash_with_user_id_sets_both() -> None:
    import uuid

    fake = FakeSession()
    user = uuid.uuid4()
    asyncio.run(set_gucs(fake, user_id=user, challenge_hash=DIGEST))  # type: ignore[arg-type]
    assert [c[0] for c in fake.calls] == ["app.user_id", "app.challenge_hash"]
