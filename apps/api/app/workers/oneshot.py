"""Run one periodic body once, now: `python -m app.workers.oneshot <task>` (S15). For the local
e2e (the refresh step) and for operators; it is a worker module, so the decrypt-path import
contract holds. The task is a literal allow-list and nothing else is accepted."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import Any, Final

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.request_context import new_request_id, request_context
from app.db.session import dispose_engine
from app.workers.periodic import prune_oauth_states, refresh_marketplace_tokens

TASKS: Final[dict[str, Callable[[], Awaitable[Any]]]] = {
    "refresh_marketplace_tokens": refresh_marketplace_tokens,
    "prune_oauth_states": prune_oauth_states,
}
USAGE: Final = (
    "usage: python -m app.workers.oneshot {refresh_marketplace_tokens|prune_oauth_states}"
)


async def _run(name: str) -> None:
    try:
        with request_context(new_request_id()):
            await TASKS[name]()
    finally:
        await dispose_engine()


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in TASKS:
        sys.stderr.write(USAGE + "\n")
        return 2
    configure_logging(get_settings())
    asyncio.run(_run(argv[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
