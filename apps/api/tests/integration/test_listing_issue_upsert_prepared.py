"""`ListingIssueRepository.upsert_open` sends the `li_one_open` predicate as a literal. A bound
`status = %(status_1)s` worked until psycopg prepared the statement; from the 11th run on one
connection Postgres planned it generically, could not infer the partial index and raised
InvalidColumnReference, so a publish job's issue sync failed after ten upserts."""

from __future__ import annotations

import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .review_support import REVIEWER, generated
from .test_listing_issue_repository import new_issue

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]


async def test_upsert_open_survives_many_runs_on_one_connection(gen_world: GenWorld) -> None:
    from app.repositories.uow import tenant_uow

    made = await generated(gen_world)
    principal = gen_world.principal(REVIEWER)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        for n in range(15):
            field = f"attributes.a{n}"
            await uow.listing_issues.upsert_open(
                new_issue(
                    made, field_path=field, dedupe_key=f"rules_engine|{field}|VALUE_NOT_ALLOWED|"
                )
            )
